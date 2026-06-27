"""Flask app — wires Repository, NichePack, ScanEngine, RedditOAuth into HTTP routes."""
from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, redirect, render_template, request, send_file, session

from cwscraper import __version__
from cwscraper.core.engine import ScanEngine
from cwscraper.core.models import PIPELINE_STAGE_LABELS, PIPELINE_STAGES
from cwscraper.core.niche import category_to_niche_map, list_bundled_niches, load_niche
from cwscraper.core.preflight import evaluate as evaluate_preflight
from cwscraper.core.scheduler import AutoScanner
from cwscraper.core.store import JSONRepository
from cwscraper.email.bulk import bulk_draft_and_queue
from cwscraper.email.dispatcher import EmailDispatcher
from cwscraper.email.inbound import (
    InboundEmailPoller,
    inbound_settings_summary,
)
from cwscraper.email.inbound_drafts import InboundDraftQueue
from cwscraper.email.queue import ScheduledEmailQueue
from cwscraper.email.send_limits import settings_summary as send_limits_summary
from cwscraper.email.suppression import SuppressionList
from cwscraper.email.transport import get_transport
from cwscraper.replies import RedditOAuth, draft_outreach, draft_reply, post_reddit_comment
from cwscraper.replies.personalizer import Personalizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cwscraper.web")


class AppContext:
    """Mutable holder for niche-dependent objects.

    Lets the dashboard swap niche packs at runtime without restarting the
    process. Routes read ctx.niche / ctx.engine on every request, so a swap
    is visible immediately.
    """

    def __init__(self):
        self.repo = JSONRepository()
        self.reddit_oauth = RedditOAuth(self.repo)
        self.niche = None
        self.engine = None
        self.scheduler: AutoScanner | None = None
        # Email scheduling — queue + background dispatcher + suppression list
        self.email_queue = ScheduledEmailQueue(self.repo.dir)
        self.suppression = SuppressionList(self.repo.dir)
        self.email_dispatcher = EmailDispatcher(
            self.email_queue, self.repo, suppression=self.suppression,
        )
        # AI personalizer — uses ANTHROPIC_API_KEY; falls back gracefully
        # when unset (returns a regional generic opener instead of failing).
        # Local JSON cache means re-drafting the same business doesn't re-bill.
        self.personalizer = Personalizer(
            cache_file=self.repo.dir / "personalizations.json",
        )
        # Queue of AI-drafted second-touch replies, awaiting operator review.
        # The inbound poller drops drafts here when a reply is classified
        # as 'interested'; the operator reviews + approves from the dashboard.
        self.inbound_drafts = InboundDraftQueue(self.repo.dir)
        # Inbound reply polling — dormant unless IMAP_ENABLED=true
        self.inbound_poller = InboundEmailPoller(
            self.repo, self.suppression,
            inbound_drafts=self.inbound_drafts,
        )
        self._lock = threading.Lock()

    def boot(self) -> None:
        """Load active niche from config (or env, or fallback) on app start."""
        cfg = self.repo.get_config()
        slug = cfg.get("active_niche") or os.getenv("CWSCRAPER_NICHE") or "caregiver"
        try:
            self.swap_niche(slug)
        except FileNotFoundError:
            logger.warning("Niche '%s' not found, falling back to caregiver", slug)
            self.swap_niche("caregiver")
        # Background email dispatcher — safe to always start; tick() no-ops
        # when the queue is empty or the transport isn't configured.
        self.email_dispatcher.start()
        # Inbound poller — same safety: ticks no-op when IMAP_ENABLED is false
        # or creds aren't set.
        self.inbound_poller.start()

    def swap_niche(self, slug: str) -> dict:
        """Switch to a different niche pack. Persists the choice."""
        with self._lock:
            if self.engine and (self.engine.is_scanning or self.engine.is_enriching):
                raise RuntimeError(
                    "Cannot switch niche while a scan or enrichment is in progress"
                )

            new_niche = load_niche(slug)

            if self.scheduler:
                self.scheduler.stop()

            self.niche = new_niche
            self.engine = ScanEngine(self.repo, new_niche)
            self.scheduler = AutoScanner(self.engine, self.repo)

            cfg = self.repo.get_config()
            cfg["active_niche"] = new_niche.slug
            self.repo.save_config(cfg)
            if cfg.get("auto_scan_enabled"):
                self.scheduler.start()

            logger.info("Active niche: %s (%s mode)", new_niche.slug, new_niche.mode)
            return {
                "slug": new_niche.slug,
                "display_name": new_niche.display_name,
                "mode": new_niche.mode,
                "description": new_niche.description,
            }


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = os.getenv("CWSCRAPER_SECRET", "cwscraper-dev-secret-change-me")
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    ctx = AppContext()
    ctx.boot()
    app.extensions["cwscraper"] = ctx

    # Convenience: short aliases the route bodies use.
    repo = ctx.repo
    reddit_oauth = ctx.reddit_oauth

    # ----------------------- no-cache for dev/admin -------------------------
    @app.after_request
    def _no_cache(resp):
        if resp.content_type and (
            "text/html" in resp.content_type or "application/json" in resp.content_type
        ):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp

    # ----------------------- core pages -------------------------------------
    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", niche=ctx.niche, version=__version__)

    @app.route("/api/health")
    def api_health():
        return jsonify({"ok": True, "version": __version__, "niche": ctx.niche.slug})

    # ----------------------- niche switching --------------------------------
    @app.route("/api/niches")
    def api_list_niches():
        """List all bundled niche packs + which one is active."""
        return jsonify({
            "active": ctx.niche.slug,
            "available": list_bundled_niches(),
        })

    @app.route("/api/niches/active", methods=["GET"])
    def api_active_niche():
        return jsonify({
            "slug": ctx.niche.slug,
            "display_name": ctx.niche.display_name,
            "mode": ctx.niche.mode,
            "description": ctx.niche.description,
        })

    @app.route("/api/preflight")
    def api_preflight():
        """Readiness check for the active niche. Surfaces blockers/warnings."""
        check = evaluate_preflight(ctx.niche)
        return jsonify({
            "niche": {
                "slug": ctx.niche.slug,
                "display_name": ctx.niche.display_name,
                "mode": ctx.niche.mode,
            },
            **check.to_dict(),
        })

    @app.route("/api/niches/active", methods=["POST"])
    def api_switch_niche():
        data = request.get_json() or {}
        slug = data.get("slug")
        if not slug:
            return jsonify({"error": "slug is required"}), 400
        try:
            result = ctx.swap_niche(slug)
        except FileNotFoundError:
            return jsonify({"error": f"Niche pack '{slug}' not found"}), 404
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"ok": True, **result})

    # ----------------------- stats ------------------------------------------
    @app.route("/api/stats")
    def api_stats():
        leads = _back_compat_leads(repo.get_leads())
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)

        high = sum(1 for l in leads if l.get("intent_level") == "high")
        medium = sum(1 for l in leads if l.get("intent_level") == "medium")
        new_count = sum(1 for l in leads if l.get("status") == "new")
        reviewed = sum(1 for l in leads if l.get("status") == "reviewed")
        contacted = sum(1 for l in leads if l.get("status") == "contacted")
        today_leads = sum(
            1 for l in leads if l.get("discovered_at", "")[:10] == today.strftime("%Y-%m-%d")
        )

        week_leads = 0
        for l in leads:
            try:
                d = datetime.fromisoformat(l.get("discovered_at", "").replace("Z", "+00:00"))
                if d >= week_ago:
                    week_leads += 1
            except (ValueError, TypeError):
                pass

        by_sub: dict[str, int] = {}
        by_platform: dict[str, int] = {}
        for l in leads:
            s = l.get("subreddit", "unknown")
            by_sub[s] = by_sub.get(s, 0) + 1
            p = l.get("platform", "reddit")
            by_platform[p] = by_platform.get(p, 0) + 1

        kw_counts: dict[str, int] = {}
        for l in leads:
            for kw in l.get("matched_keywords", []):
                kw_counts[kw] = kw_counts.get(kw, 0) + 1
        top_keywords = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:15]

        logs = repo.get_scan_logs()
        return jsonify({
            "total_leads": len(leads),
            "high_intent": high,
            "medium_intent": medium,
            "new": new_count,
            "reviewed": reviewed,
            "contacted": contacted,
            "today": today_leads,
            "this_week": week_leads,
            "by_subreddit": dict(sorted(by_sub.items(), key=lambda x: x[1], reverse=True)),
            "by_platform": dict(sorted(by_platform.items(), key=lambda x: x[1], reverse=True)),
            "top_keywords": top_keywords,
            "last_scan": logs[0] if logs else None,
            "is_scanning": ctx.engine.is_scanning,
            "total_scans": len(logs),
        })

    # ----------------------- leads ------------------------------------------
    @app.route("/api/leads")
    def api_leads():
        leads = _back_compat_leads(repo.get_leads())
        intent = request.args.get("intent")
        status = request.args.get("status")
        source = request.args.get("subreddit") or request.args.get("source")
        search = request.args.get("search", "").lower()

        if intent:
            leads = [l for l in leads if l.get("intent_level") == intent]
        if status:
            leads = [l for l in leads if l.get("status") == status]
        if source:
            leads = [l for l in leads if l.get("subreddit") == source]
        if search:
            leads = [
                l for l in leads
                if search in l.get("title", "").lower()
                or search in l.get("selftext_preview", "").lower()
            ]
        leads.sort(key=lambda x: x.get("discovered_at", ""), reverse=True)

        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 25))
        start = (page - 1) * per_page
        return jsonify({
            "leads": leads[start:start + per_page],
            "total": len(leads),
            "page": page,
            "per_page": per_page,
            "pages": (len(leads) + per_page - 1) // per_page,
        })

    @app.route("/api/leads/<lead_id>/status", methods=["POST"])
    def api_update_lead_status(lead_id):
        data = request.get_json() or {}
        status = data.get("status")
        if status not in ("new", "reviewed", "contacted", "dismissed"):
            return jsonify({"error": "Invalid status"}), 400
        repo.update_lead_status(lead_id, status)
        return jsonify({"ok": True})

    # ----------------------- scanning ---------------------------------------
    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        if ctx.engine.is_scanning:
            return jsonify({"error": "Scan already in progress"}), 409
        # Optional client-side hint — refuse if caller expected a different mode.
        data = request.get_json(silent=True) or {}
        expected_mode = data.get("expected_mode")
        if expected_mode and expected_mode != ctx.niche.mode:
            return jsonify({
                "error": (
                    f"Active niche '{ctx.niche.slug}' is in {ctx.niche.mode} mode, "
                    f"but caller requested {expected_mode} mode. Switch niches first."
                ),
                "active_niche": ctx.niche.slug,
                "active_mode": ctx.niche.mode,
            }), 409
        threading.Thread(target=ctx.engine.run_full_scan, daemon=True).start()
        return jsonify({"status": "started", "mode": ctx.niche.mode})

    @app.route("/api/scan/status")
    def api_scan_status():
        return jsonify({
            "is_scanning": ctx.engine.is_scanning,
            "last_scan": ctx.engine.last_scan,
            "progress": ctx.engine.progress,
        })

    @app.route("/api/crm/push-all", methods=["POST"])
    def api_crm_push_all():
        """Resumable backfill: push stored businesses to the SGiQ CRM as Companies.

        Processes businesses[offset:offset+limit] SYNCHRONOUSLY and returns
        progress, so a caller can drive it to completion. (A background thread is
        unreliable here — gunicorn recycles the worker mid-run and every call
        would restart from the beginning, so only the first chunk ever lands.)
        Idempotent (external_id = source:id) — re-runs merge rather than duplicate.

        Query params: ?offset=0&limit=50
        """
        if not (os.getenv("SGIQ_CRM_URL") and os.getenv("SGIQ_CRM_API_KEY")):
            return jsonify({
                "error": "Set SGIQ_CRM_URL and SGIQ_CRM_API_KEY to enable the CRM push.",
            }), 400
        from cwscraper.integrations.crm import push_businesses

        businesses = ctx.repo.get_businesses()
        total = len(businesses)
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = max(1, min(500, int(request.args.get("limit", 50))))
        except (TypeError, ValueError):
            offset, limit = 0, 50

        chunk = businesses[offset:offset + limit]
        push_businesses(chunk)  # synchronous — sized to finish within the request
        next_offset = offset + len(chunk)
        return jsonify({
            "pushed": len(chunk),
            "offset": offset,
            "next_offset": next_offset,
            "total": total,
            "done": next_offset >= total,
        })

    @app.route("/api/discover", methods=["POST"])
    def api_discover():
        if ctx.engine.is_scanning:
            return jsonify({"error": "Scanner is busy"}), 409
        from cwscraper.scanners.base import ScannerContext
        from cwscraper.scanners.reddit import RedditScanner

        rs = RedditScanner(ctx.niche)
        queries = ctx.niche.medium_intent_keywords[:5] or ["caregiver"]
        scan_ctx = ScannerContext()
        return jsonify({"subreddits": rs.discover_subreddits(queries, scan_ctx)})

    # ----------------------- config -----------------------------------------
    @app.route("/api/config", methods=["GET"])
    def api_get_config():
        cfg = repo.get_config()
        # back-compat keys the existing dashboard reads
        cfg.setdefault("subreddits", [asdict(s) for s in ctx.niche.subreddits])
        cfg.setdefault("quora_enabled", False)
        cfg.setdefault("agingcare_enabled", False)
        return jsonify(cfg)

    @app.route("/api/config", methods=["POST"])
    def api_save_config():
        data = request.get_json() or {}
        cfg = repo.get_config()
        cfg.update(data)
        repo.save_config(cfg)
        if cfg.get("auto_scan_enabled"):
            ctx.scheduler.start()
        else:
            ctx.scheduler.stop()
        return jsonify({"ok": True})

    @app.route("/api/directory")
    def api_directory():
        return jsonify({
            "reddit": [
                {
                    "name": s.name,
                    "category": s.category,
                    "enabled": s.enabled,
                    "url": f"https://reddit.com/r/{s.name}",
                    "platform": "Reddit",
                }
                for s in ctx.niche.subreddits
            ],
            "facebook": [],
            "other": [],
        })

    @app.route("/api/scan-logs")
    def api_scan_logs():
        return jsonify(repo.get_scan_logs()[:50])

    # ----------------------- export -----------------------------------------
    @app.route("/api/export/csv")
    def api_export_csv():
        leads = _back_compat_leads(repo.get_leads())
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "id", "subreddit", "title", "url", "score", "num_comments",
                "intent_level", "matched_keywords", "status", "discovered_at",
            ],
        )
        writer.writeheader()
        for lead in leads:
            row = {k: lead.get(k, "") for k in writer.fieldnames}
            row["matched_keywords"] = ", ".join(lead.get("matched_keywords", []))
            writer.writerow(row)
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode()),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"cwscraper_leads_{datetime.now().strftime('%Y%m%d')}.csv",
        )

    # ----------------------- replies ----------------------------------------
    @app.route("/api/replies")
    def api_replies():
        replies = repo.get_replies()
        status_filter = request.args.get("status")
        if status_filter:
            replies = [r for r in replies if r.get("status") == status_filter]
        return jsonify({"replies": replies})

    @app.route("/api/replies/draft", methods=["POST"])
    def api_generate_draft():
        data = request.get_json() or {}
        lead_id = data.get("lead_id")
        lead = next((l for l in repo.get_leads() if l.get("id") == lead_id), None)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        draft = draft_reply(lead, ctx.niche)
        repo.save_reply(draft)
        return jsonify(draft)

    @app.route("/api/replies/save", methods=["POST"])
    def api_save_edited_draft():
        data = request.get_json() or {}
        lead_id = data.get("lead_id")
        edited = data.get("draft_text")
        if not lead_id or not edited:
            return jsonify({"error": "lead_id and draft_text required"}), 400
        existing = next((r for r in repo.get_replies() if r.get("lead_id") == lead_id), None)
        if existing:
            existing["draft_text"] = edited
            repo.save_reply(existing)
        else:
            repo.save_reply({
                "lead_id": lead_id,
                "draft_text": edited,
                "template_used": "custom",
                "template_name": "Custom",
            })
        return jsonify({"ok": True})

    @app.route("/api/replies/send", methods=["POST"])
    def api_send_reply():
        data = request.get_json() or {}
        lead_id = data.get("lead_id")
        text = data.get("text")
        if not lead_id or not text:
            return jsonify({"error": "lead_id and text required"}), 400
        lead = next((l for l in repo.get_leads() if l.get("id") == lead_id), None)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        result = post_reddit_comment(reddit_oauth, lead["url"], text)
        if result.get("success"):
            repo.update_reply_status(lead_id, "sent")
            repo.update_lead_status(lead_id, "contacted")
            return jsonify({"ok": True, "message": "Reply posted to Reddit"})
        return jsonify({"error": result.get("error", "Unknown error")}), 400

    @app.route("/api/replies/open", methods=["POST"])
    def api_open_in_browser():
        data = request.get_json() or {}
        url = data.get("url")
        if url:
            webbrowser.open(url)
            return jsonify({"ok": True})
        return jsonify({"error": "No URL"}), 400

    @app.route("/api/replies/templates")
    def api_reply_templates():
        return jsonify({
            t.key: {"name": t.name, "template": t.template}
            for t in ctx.niche.reply_templates
        })

    # ----------------------- businesses (directory mode) -------------------
    @app.route("/api/businesses")
    def api_businesses():
        businesses = repo.get_businesses()
        status = request.args.get("status")
        state = request.args.get("state")
        city = request.args.get("city")
        niche = request.args.get("niche")
        has_website = request.args.get("has_website")
        has_email = request.args.get("has_email")
        min_rating = request.args.get("min_rating", type=float)
        search = request.args.get("search", "").lower()

        if status:
            businesses = [b for b in businesses if b.get("status") == status]
        if state:
            businesses = [b for b in businesses if b.get("state") == state]
        if city:
            businesses = [b for b in businesses if b.get("city", "").lower() == city.lower()]
        if niche:
            businesses = [b for b in businesses if niche in (b.get("source_niches") or [])]
        if has_website in ("true", "1"):
            businesses = [b for b in businesses if b.get("website")]
        if has_email in ("true", "1"):
            businesses = [b for b in businesses if b.get("email")]
        if min_rating is not None:
            businesses = [b for b in businesses if (b.get("rating") or 0) >= min_rating]
        if search:
            businesses = [
                b for b in businesses
                if search in b.get("name", "").lower()
                or search in b.get("address", "").lower()
                or search in b.get("city", "").lower()
            ]

        businesses.sort(
            key=lambda b: (-(b.get("rating") or 0), -(b.get("review_count") or 0))
        )

        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 25))
        start_idx = (page - 1) * per_page
        return jsonify({
            "businesses": businesses[start_idx:start_idx + per_page],
            "total": len(businesses),
            "page": page,
            "per_page": per_page,
            "pages": (len(businesses) + per_page - 1) // per_page,
        })

    @app.route("/api/businesses/<business_id>/status", methods=["POST"])
    def api_update_business_status(business_id):
        data = request.get_json() or {}
        status = data.get("status")
        if status not in ("new", "qualified", "contacted", "dismissed"):
            return jsonify({"error": "Invalid status"}), 400
        repo.update_business_status(business_id, status)
        return jsonify({"ok": True})

    @app.route("/api/businesses/<business_id>/contact", methods=["POST"])
    def api_update_business_contact(business_id):
        """Manually set contact info on a business lead.

        Body: {email?: str, contacts?: [{name, title, email, phone}]}
        Empty string for `email` clears the field (so it can be re-enriched).
        """
        import re as _re
        data = request.get_json() or {}
        patch = {}
        if "email" in data:
            email = (data["email"] or "").strip().lower()
            if email and not _re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$", email):
                return jsonify({"error": f"Invalid email format: {email!r}"}), 400
            patch["email"] = email
        if "contacts" in data:
            if not isinstance(data["contacts"], list):
                return jsonify({"error": "contacts must be an array"}), 400
            cleaned = []
            for c in data["contacts"]:
                if not isinstance(c, dict):
                    continue
                email = (c.get("email") or "").strip().lower()
                if email and not _re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$", email):
                    return jsonify({"error": f"Invalid email format in contacts: {email!r}"}), 400
                cleaned.append({
                    "name":  (c.get("name") or "").strip(),
                    "title": (c.get("title") or "").strip(),
                    "email": email,
                    "phone": (c.get("phone") or "").strip(),
                    "source_url": (c.get("source_url") or "manual").strip(),
                })
            patch["contacts"] = cleaned
        if not patch:
            return jsonify({"error": "Provide email and/or contacts"}), 400
        repo.update_business(business_id, patch)
        return jsonify({"ok": True, "patch": patch})

    @app.route("/api/businesses/<business_id>/enrich", methods=["POST"])
    def api_enrich_one_business(business_id):
        """Run an enricher on a single business synchronously and return the result.

        Body: {enricher?: 'website'|'playwright', clear_first?: bool}
        clear_first=true wipes the existing email/contacts so the enricher
        actually re-runs (otherwise only_missing_email=true would skip it).
        """
        from cwscraper.enrichment import ALL_ENRICHERS
        from cwscraper.enrichment.base import EnrichmentContext

        data = request.get_json(silent=True) or {}
        enricher_slug = data.get("enricher", "website")
        clear_first = bool(data.get("clear_first", True))

        if enricher_slug not in ALL_ENRICHERS:
            return jsonify({
                "error": f"Unknown enricher '{enricher_slug}'. Available: {sorted(ALL_ENRICHERS)}"
            }), 400

        biz = next((b for b in repo.get_businesses() if b.get("id") == business_id), None)
        if not biz:
            return jsonify({"error": "Business not found"}), 404
        if not biz.get("website"):
            return jsonify({"error": "Business has no website to scrape"}), 422

        if clear_first:
            repo.update_business(business_id, {"email": "", "contacts": []})
            biz["email"] = ""
            biz["contacts"] = []

        scraper = ALL_ENRICHERS[enricher_slug]()
        enrich_ctx = EnrichmentContext(businesses_total=1)
        try:
            result = scraper.enrich(biz, enrich_ctx)
        except Exception as e:
            return jsonify({"error": f"Enrichment failed: {e}"}), 500

        if result and (result.email or result.contacts):
            patch = {}
            if result.email:
                patch["email"] = result.email
            if result.contacts:
                patch["contacts"] = result.contacts
            repo.update_business(business_id, patch)
            return jsonify({
                "ok": True,
                "email": result.email,
                "contacts_found": len(result.contacts),
                "contacts": result.contacts,
            })

        return jsonify({
            "ok": True,
            "email": "",
            "contacts_found": 0,
            "contacts": [],
            "message": "Enricher ran but found no contacts",
            "errors": enrich_ctx.errors,
        })

    @app.route("/api/businesses/scrub-contact-names", methods=["POST"])
    def api_business_scrub_contact_names():
        """One-shot: clear junk contact names that fail _looks_like_name().

        Use after deploying changes to the name filter (e.g. PR #6) to clean
        up stale junk names already in the JSON store. For each contact on
        each business, if its name fails the current filter, the name is
        replaced with the empty string. The email and other contact fields
        are preserved — drafts will then greet the recipient with "Hi there,"
        instead of "Hi Accessibility Tools Accessibility,".

        Body (optional):
          {"dry_run": true}  — only count, don't modify

        Returns:
          {
            "ok": true, "dry_run": bool,
            "businesses_scanned": int,
            "contacts_scanned": int,
            "names_cleared": int,
            "businesses_affected": int,
            "sample": [{"business_name", "old_name"}, ...]   # up to 25
          }
        """
        from cwscraper.enrichment.website_scraper import _looks_like_name
        payload = request.get_json(silent=True) or {}
        dry_run = bool(payload.get("dry_run", False))

        businesses = repo.get_businesses()
        contacts_scanned = 0
        names_cleared = 0
        businesses_affected = 0
        sample: list[dict] = []

        for b in businesses:
            contacts = b.get("contacts") or []
            if not contacts:
                continue
            biz_modified = False
            for c in contacts:
                contacts_scanned += 1
                name = (c.get("name") or "").strip()
                if not name:
                    continue
                if _looks_like_name(name):
                    continue
                if len(sample) < 25:
                    sample.append({
                        "business_name": b.get("name", ""),
                        "old_name": name,
                    })
                names_cleared += 1
                biz_modified = True
                if not dry_run:
                    c["name"] = ""
            if biz_modified:
                businesses_affected += 1

        if not dry_run and businesses_affected:
            # Write back via the repo's businesses_file directly — there's
            # no public bulk-write method but the underlying JSON is a
            # plain list and update_business() would be N round trips.
            import json as _json
            with repo._lock:
                repo.businesses_file.write_text(
                    _json.dumps(businesses, indent=2), encoding="utf-8"
                )

        return jsonify({
            "ok": True,
            "dry_run": dry_run,
            "businesses_scanned": len(businesses),
            "contacts_scanned": contacts_scanned,
            "names_cleared": names_cleared,
            "businesses_affected": businesses_affected,
            "sample": sample,
        })

    @app.route("/api/businesses/backfill-niches", methods=["POST"])
    def api_business_backfill_niches():
        """One-shot: stamp source_niches onto rows that lack the tag.

        Uses each row's `category` field (set by the scanner when discovered)
        to reverse-lookup which niche pack discovered it. Returns a summary
        of how many got tagged and how many couldn't be matched.

        Idempotent: businesses that already have source_niches are skipped.
        Safe to call repeatedly.
        """
        cat_to_niche = category_to_niche_map()
        businesses = repo.get_businesses()

        tagged = 0
        unmatched_categories: dict[str, int] = {}
        skipped_already_tagged = 0
        # Build the new full list in-memory, write once at the end
        for b in businesses:
            if b.get("source_niches"):
                skipped_already_tagged += 1
                continue
            cat = (b.get("category") or "").strip()
            niche_slug = cat_to_niche.get(cat)
            if niche_slug:
                b["source_niches"] = [niche_slug]
                tagged += 1
            else:
                unmatched_categories[cat or "(empty)"] = (
                    unmatched_categories.get(cat or "(empty)", 0) + 1
                )

        # Write back via the repo (no public bulk-set method; reach into the
        # JSON file directly since this is admin-only).
        if tagged:
            import json as _json
            repo.businesses_file.write_text(
                _json.dumps(businesses, indent=2), encoding="utf-8"
            )

        return jsonify({
            "ok": True,
            "tagged": tagged,
            "skipped_already_tagged": skipped_already_tagged,
            "unmatched": [
                {"category": c, "count": n}
                for c, n in sorted(unmatched_categories.items(), key=lambda x: -x[1])
            ],
            "category_to_niche_map": cat_to_niche,
        })

    @app.route("/api/businesses/niches")
    def api_business_niches():
        """Distinct source_niches across the current business dataset, with counts.

        Powers the niche filter dropdown on the Businesses tab — only shows
        niches the user actually has data for.
        """
        counts: dict[str, int] = {}
        untagged = 0
        for b in repo.get_businesses():
            niches = b.get("source_niches") or []
            if not niches:
                untagged += 1
                continue
            for n in niches:
                counts[n] = counts.get(n, 0) + 1
        return jsonify({
            "niches": [
                {"slug": slug, "count": counts[slug]}
                for slug in sorted(counts, key=lambda s: (-counts[s], s))
            ],
            "untagged": untagged,
        })

    @app.route("/api/businesses/stats")
    def api_business_stats():
        businesses = repo.get_businesses()
        by_state: dict[str, int] = {}
        by_status: dict[str, int] = {"new": 0, "qualified": 0, "contacted": 0, "dismissed": 0}
        with_website = with_phone = with_email = 0
        for b in businesses:
            by_state[b.get("state", "")] = by_state.get(b.get("state", ""), 0) + 1
            by_status[b.get("status", "new")] = by_status.get(b.get("status", "new"), 0) + 1
            if b.get("website"):
                with_website += 1
            if b.get("phone"):
                with_phone += 1
            if b.get("email"):
                with_email += 1
        return jsonify({
            "total": len(businesses),
            "by_state": dict(sorted(by_state.items())),
            "by_status": by_status,
            "with_website": with_website,
            "with_phone": with_phone,
            "with_email": with_email,
        })

    @app.route("/api/businesses/export/csv")
    def api_businesses_csv():
        businesses = repo.get_businesses()
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "name", "category", "address", "city", "state", "zip_code",
                "phone", "website", "email", "rating", "review_count",
                "discovered_via", "status", "discovered_at",
            ],
        )
        writer.writeheader()
        for b in businesses:
            writer.writerow({k: b.get(k, "") for k in writer.fieldnames})
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode()),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"cwscraper_businesses_{datetime.now().strftime('%Y%m%d')}.csv",
        )

    # ----------------------- pipeline / CRM-lite ---------------------------
    @app.route("/api/pipeline/config")
    def api_pipeline_config():
        return jsonify({
            "stages": list(PIPELINE_STAGES),
            "stage_labels": PIPELINE_STAGE_LABELS,
        })

    @app.route("/api/prospects")
    def api_prospects():
        """Unified list spanning leads + businesses, optional stage/tag/type filters."""
        prospects = repo.get_all_prospects()
        stage = request.args.get("stage")
        tag = request.args.get("tag")
        lead_type = request.args.get("lead_type")
        search = request.args.get("search", "").lower()
        overdue_only = request.args.get("overdue") in ("1", "true")

        if stage:
            prospects = [p for p in prospects if p.get("pipeline_stage") == stage]
        if tag:
            prospects = [p for p in prospects if tag in (p.get("tags") or [])]
        if lead_type:
            prospects = [p for p in prospects if p.get("lead_type") == lead_type]
        if search:
            def _hay(p):
                return " ".join([
                    p.get("title", ""), p.get("name", ""),
                    p.get("notes", ""), p.get("source", ""),
                ]).lower()
            prospects = [p for p in prospects if search in _hay(p)]
        if overdue_only:
            today = datetime.now(timezone.utc).date().isoformat()
            prospects = [
                p for p in prospects
                if p.get("follow_up_date") and p["follow_up_date"] <= today
                and p.get("pipeline_stage") not in ("customer", "lost")
            ]

        # Sort: overdue follow-ups first, then by stage order, then newest discovered.
        # Python's sort is stable, so apply secondary criterion (newest first) then primary.
        stage_order = {s: i for i, s in enumerate(PIPELINE_STAGES)}
        today_str = datetime.now(timezone.utc).date().isoformat()

        def _is_overdue(p):
            return bool(
                p.get("follow_up_date")
                and p["follow_up_date"] <= today_str
                and p.get("pipeline_stage") not in ("customer", "lost")
            )

        prospects.sort(key=lambda p: p.get("discovered_at", ""), reverse=True)
        prospects.sort(key=lambda p: (
            0 if _is_overdue(p) else 1,
            stage_order.get(p.get("pipeline_stage", "new"), 99),
        ))
        return jsonify({"prospects": prospects, "total": len(prospects)})

    @app.route("/api/pipeline/stats")
    def api_pipeline_stats():
        prospects = repo.get_all_prospects()
        by_stage = {s: 0 for s in PIPELINE_STAGES}
        community = business = 0
        today = datetime.now(timezone.utc).date().isoformat()
        overdue = 0
        for p in prospects:
            stage = p.get("pipeline_stage", "new")
            by_stage[stage] = by_stage.get(stage, 0) + 1
            if p.get("lead_type") == "community":
                community += 1
            else:
                business += 1
            if (p.get("follow_up_date") and p["follow_up_date"] <= today
                    and stage not in ("customer", "lost")):
                overdue += 1
        return jsonify({
            "total": len(prospects),
            "community": community,
            "business": business,
            "overdue_follow_ups": overdue,
            "by_stage": by_stage,
            "stage_labels": PIPELINE_STAGE_LABELS,
        })

    @app.route("/api/prospects/<prospect_id>/stage", methods=["POST"])
    def api_set_stage(prospect_id):
        data = request.get_json() or {}
        stage = data.get("stage")
        lead_type = data.get("lead_type", "business")
        if stage not in PIPELINE_STAGES:
            return jsonify({"error": f"Invalid stage. Must be one of: {list(PIPELINE_STAGES)}"}), 400
        result = repo.update_prospect(
            prospect_id, lead_type, {"pipeline_stage": stage}, action="stage_change"
        )
        if not result:
            return jsonify({"error": "Prospect not found"}), 404
        return jsonify({"ok": True, "prospect": result})

    @app.route("/api/prospects/<prospect_id>/notes", methods=["POST"])
    def api_set_notes(prospect_id):
        data = request.get_json() or {}
        notes = data.get("notes", "")
        lead_type = data.get("lead_type", "business")
        result = repo.update_prospect(
            prospect_id, lead_type, {"notes": notes}, action="notes_updated"
        )
        if not result:
            return jsonify({"error": "Prospect not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/prospects/<prospect_id>/follow-up", methods=["POST"])
    def api_set_follow_up(prospect_id):
        data = request.get_json() or {}
        follow_up = data.get("follow_up_date", "")
        lead_type = data.get("lead_type", "business")
        # Validate ISO date if provided
        if follow_up:
            try:
                datetime.strptime(follow_up, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "follow_up_date must be YYYY-MM-DD"}), 400
        result = repo.update_prospect(
            prospect_id, lead_type, {"follow_up_date": follow_up}, action="follow_up_set"
        )
        if not result:
            return jsonify({"error": "Prospect not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/prospects/<prospect_id>/tags", methods=["POST"])
    def api_set_tags(prospect_id):
        data = request.get_json() or {}
        tags = data.get("tags")
        lead_type = data.get("lead_type", "business")
        if not isinstance(tags, list):
            return jsonify({"error": "tags must be an array of strings"}), 400
        cleaned = sorted({str(t).strip().lower() for t in tags if str(t).strip()})
        result = repo.update_prospect(
            prospect_id, lead_type, {"tags": cleaned}, action="tags_updated"
        )
        if not result:
            return jsonify({"error": "Prospect not found"}), 404
        return jsonify({"ok": True, "tags": cleaned})

    @app.route("/api/prospects/<prospect_id>")
    def api_get_prospect(prospect_id):
        """Return a single prospect with full activity log."""
        for p in repo.get_all_prospects():
            if p.get("id") == prospect_id:
                return jsonify(p)
        return jsonify({"error": "Prospect not found"}), 404

    # ----------------------- enrichment ------------------------------------
    @app.route("/api/enrich", methods=["POST"])
    def api_enrich():
        if ctx.engine.is_enriching:
            return jsonify({"error": "Enrichment already in progress"}), 409
        data = request.get_json(silent=True) or {}
        enricher = data.get("enricher", "website")
        only_missing = data.get("only_missing_email", True)
        limit = data.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                return jsonify({"error": "limit must be an integer"}), 400

        from cwscraper.enrichment import ALL_ENRICHERS
        if enricher not in ALL_ENRICHERS:
            return jsonify({
                "error": f"Unknown enricher '{enricher}'. Available: {sorted(ALL_ENRICHERS)}"
            }), 400

        threading.Thread(
            target=ctx.engine.run_enrichment,
            kwargs={
                "enricher_slug": enricher,
                "only_missing_email": bool(only_missing),
                "limit": limit,
            },
            daemon=True,
        ).start()
        return jsonify({"status": "started"})

    @app.route("/api/enrich/status")
    def api_enrich_status():
        return jsonify({
            "is_enriching": ctx.engine.is_enriching,
            "progress": ctx.engine.enrichment_progress,
            "last_enrichment": ctx.engine.last_enrichment,
        })

    @app.route("/api/enrich/stop", methods=["POST"])
    def api_enrich_stop():
        """Signal the running enrichment loop to stop after the current item.

        In-flight Playwright pages finish their current goto/render and then
        exit — typical real-world latency is 1-10 seconds before idle.
        """
        if not ctx.engine.is_enriching:
            return jsonify({"error": "No enrichment in progress"}), 409
        ctx.engine.cancel_enrichment()
        return jsonify({"ok": True, "status": "cancelling"})

    # ----------------------- email scheduling ------------------------------

    @app.route("/api/emails/transport")
    def api_email_transport_status():
        """Tell the dashboard whether scheduling is wired up."""
        t = get_transport()
        return jsonify({
            "configured": t is not None,
            "transport": t.name if t else None,
            "from_email": os.getenv("CWSCRAPER_FROM_EMAIL", ""),
            "from_name": os.getenv("CWSCRAPER_FROM_NAME", ""),
        })

    @app.route("/api/emails/scheduled")
    def api_emails_list():
        status = request.args.get("status")
        prospect_id = request.args.get("prospect_id")
        return jsonify({
            "emails": ctx.email_queue.list(status=status, prospect_id=prospect_id),
        })

    @app.route("/api/emails/schedule", methods=["POST"])
    def api_emails_schedule():
        """Enqueue one email for later send.

        Body: {
          prospect_id, lead_type ('business'|'community'),
          to_email, subject, body,
          scheduled_for ('now' or ISO YYYY-MM-DDTHH:MM:SS),
          from_email?, from_name?, reply_to?
        }
        """
        data = request.get_json() or {}

        required = ("prospect_id", "to_email", "subject", "body")
        missing = [k for k in required if not data.get(k)]
        if missing:
            return jsonify({"error": f"Missing required fields: {missing}"}), 400

        # Validate the destination email
        import re as _re
        to_email = data["to_email"].strip().lower()
        if not _re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$", to_email):
            return jsonify({"error": f"Invalid to_email: {to_email!r}"}), 400

        # Normalize scheduled_for: 'now' means right now (will fire on next tick).
        # Note: only lower-case for the 'now' alias check — Date.toISOString() in
        # the dashboard emits an uppercase 'Z' suffix that datetime.fromisoformat
        # only recognizes in upper case, so we must preserve case before parsing.
        scheduled_for_raw = (data.get("scheduled_for") or "now").strip()
        if scheduled_for_raw.lower() in ("now", ""):
            scheduled_for = datetime.now(timezone.utc).isoformat()
        else:
            try:
                # Accept either ISO datetime or YYYY-MM-DDTHH:MM (local-time-ish);
                # treat naive datetimes as UTC for predictability.
                parsed = datetime.fromisoformat(scheduled_for_raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                scheduled_for = parsed.isoformat()
            except ValueError:
                return jsonify({
                    "error": f"Invalid scheduled_for: {scheduled_for_raw!r}. "
                             "Use 'now' or an ISO datetime like 2026-05-20T14:00:00."
                }), 400

        lead_type = data.get("lead_type") or "business"
        if lead_type not in ("business", "community"):
            return jsonify({"error": "lead_type must be 'business' or 'community'"}), 400

        entry = ctx.email_queue.enqueue(
            prospect_id=data["prospect_id"],
            lead_type=lead_type,
            to_email=to_email,
            subject=data["subject"],
            body=data["body"],
            scheduled_for=scheduled_for,
            from_email=(data.get("from_email") or "").strip(),
            from_name=(data.get("from_name") or "").strip(),
            reply_to=(data.get("reply_to") or "").strip(),
        )
        return jsonify({"ok": True, "email": entry})

    @app.route("/api/emails/send-external", methods=["POST"])
    def api_emails_send_external():
        """Authed send entry point for the SGiQ CRM.

        Shared-secret auth via the CRM_INBOUND_KEY env var (X-CRM-Key header,
        constant-time compare, fail-closed). The CRM composes the message; we own
        transport, suppression, send-limits/warmup, and unsubscribe.

        Body: {to, subject, body_text, from?, reply_to?, crm_contact_id?, send_at?}
        """
        import hmac
        import re as _re
        import uuid as _uuid
        from cwscraper.email.send_limits import check_can_queue

        expected = (os.getenv("CRM_INBOUND_KEY") or "").strip()
        presented = (request.headers.get("X-CRM-Key") or "").strip()
        if not expected or not presented or not hmac.compare_digest(expected, presented):
            return jsonify({"status": "unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        to_email = (data.get("to") or "").strip().lower()
        subject = (data.get("subject") or "").strip()
        body_text = data.get("body_text") or ""
        # Optional HTML body — when present the transport sends multipart/alternative
        # (body_text is the plain-text fallback, still required).
        body_html = data.get("body_html") or ""
        if not to_email or not subject or not body_text:
            return jsonify({"status": "error", "error": "to, subject, body_text required"}), 400
        if not _re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$", to_email):
            return jsonify({"status": "error", "error": f"invalid to: {to_email!r}"}), 400

        # Transactional sends (e.g. CRM "new form" alerts) bypass cold-email
        # suppression + send caps so an internal notification always goes out.
        transactional = bool(data.get("transactional"))

        if not transactional:
            # Suppression (unsubscribes/bounces/manual) — 200 so the CRM records it.
            if ctx.suppression and ctx.suppression.is_suppressed(to_email):
                return jsonify({"status": "suppressed"}), 200

            # Send-limits / warmup (daily cap, per-domain cap).
            allowed, reason = check_can_queue(to_email, ctx.email_queue)
            if not allowed:
                return jsonify({"status": "rate_limited", "reason": reason}), 429

        sched_raw = (data.get("send_at") or "now").strip()
        if sched_raw.lower() in ("now", ""):
            scheduled_for = datetime.now(timezone.utc).isoformat()
        else:
            try:
                parsed = datetime.fromisoformat(sched_raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                scheduled_for = parsed.isoformat()
            except ValueError:
                return jsonify({"status": "error", "error": f"invalid send_at: {sched_raw!r}"}), 400

        crm_contact_id = (data.get("crm_contact_id") or "").strip()
        prospect_id = f"crm:{crm_contact_id}" if crm_contact_id else f"crm:{_uuid.uuid4().hex}"

        # Optional AI personalization: fill a {{personalized_opener}} token using
        # the same Haiku personalizer the bulk drafter uses. The CRM passes
        # `personalize: true` + a `personalize_context` ({name, city, state}).
        # Falls back to a generic regional opener when ANTHROPIC_API_KEY is unset.
        if data.get("personalize") and "personalized_opener" in body_text:
            pctx = data.get("personalize_context") or {}
            business = {
                "id": prospect_id,
                "name": (pctx.get("name") or "").strip(),
                "city": (pctx.get("city") or "").strip(),
                "state": (pctx.get("state") or "").strip(),
                "category": (pctx.get("category") or "").strip(),
            }
            try:
                opener = ctx.personalizer.personalize(business, ctx.niche).opener or ""
            except Exception:  # noqa: BLE001 — never fail a send on personalization
                opener = ""
            body_text = body_text.replace("{{personalized_opener}}", opener).replace("{personalized_opener}", opener)
            if body_html:
                body_html = body_html.replace("{{personalized_opener}}", opener).replace("{personalized_opener}", opener)

        entry = ctx.email_queue.enqueue(
            prospect_id=prospect_id,
            lead_type="external",
            to_email=to_email,
            subject=subject,
            body=body_text,
            body_html=body_html,
            scheduled_for=scheduled_for,
            from_email=(data.get("from") or os.getenv("CWSCRAPER_FROM_EMAIL") or "").strip(),
            from_name=os.getenv("CWSCRAPER_FROM_NAME", "").strip(),
            reply_to=(data.get("reply_to") or "").strip(),
        )
        return jsonify({"status": "queued", "id": entry["id"]}), 200

    @app.route("/api/emails/<email_id>/cancel", methods=["POST"])
    def api_emails_cancel(email_id):
        result = ctx.email_queue.cancel(email_id)
        if not result:
            return jsonify({
                "error": "Email not found or not pending (only pending emails can be cancelled)"
            }), 404
        return jsonify({"ok": True, "email": result})

    @app.route("/api/emails/<email_id>/send-now", methods=["POST"])
    def api_emails_send_now(email_id):
        """Move a pending email's scheduled_for to now, so the next tick sends it.

        Useful for 'I want to send this RIGHT NOW' instead of waiting for the
        scheduled time. Returns 404 if already sent/cancelled/failed.
        """
        entry = ctx.email_queue.get(email_id)
        if not entry or entry.get("status") != "pending":
            return jsonify({"error": "Email not found or not pending"}), 404
        # Patch scheduled_for to now and let the dispatcher pick it up.
        # We piggyback on _patch by calling enqueue+cancel? Cleaner: a dedicated
        # method on the queue. Quick path: dispatcher.tick() right now.
        ctx.email_queue._patch(email_id, {
            "scheduled_for": datetime.now(timezone.utc).isoformat()
        })
        # Trigger an immediate tick in the dispatcher's thread (best-effort)
        threading.Thread(
            target=ctx.email_dispatcher.tick, daemon=True
        ).start()
        return jsonify({"ok": True})

    # ----------------------- outreach (cold email drafts) ------------------
    @app.route("/api/outreach/draft", methods=["POST"])
    def api_outreach_draft():
        data = request.get_json() or {}
        business_id = data.get("business_id")
        template_key = data.get("template_key")
        business = next(
            (b for b in repo.get_businesses() if b.get("id") == business_id), None
        )
        if not business:
            return jsonify({"error": "Business not found"}), 404
        # Pass the personalizer so single-business drafts get the same
        # AI-generated opener that bulk drafts do (when the template uses
        # {personalized_opener}). Without this, the single-draft endpoint
        # returned the template with an empty opener slot.
        return jsonify(draft_outreach(
            business, ctx.niche, template_key,
            personalizer=ctx.personalizer,
        ))

    @app.route("/api/outreach/templates")
    def api_outreach_templates():
        return jsonify({
            t.key: {"name": t.name, "subject": t.subject, "body": t.body}
            for t in ctx.niche.outreach_templates
        })

    # ----------------------- Reddit OAuth -----------------------------------
    @app.route("/auth/reddit")
    def reddit_auth():
        if not reddit_oauth.configured:
            return jsonify({
                "error": "REDDIT_CLIENT_ID/SECRET not set. See .env.example."
            }), 400
        state = secrets.token_urlsafe(32)
        session["reddit_oauth_state"] = state
        return redirect(reddit_oauth.authorize_url(state))

    @app.route("/auth/reddit/callback")
    def reddit_callback():
        if request.args.get("error"):
            return f"Reddit auth error: {request.args['error']}", 400
        if request.args.get("state") != session.get("reddit_oauth_state"):
            return "Invalid state parameter", 400
        code = request.args.get("code")
        if not code:
            return "No authorization code", 400
        if not reddit_oauth.exchange_code(code):
            return "Token exchange failed", 400
        return redirect("/?tab=replies&auth=success")

    @app.route("/api/auth/reddit/status")
    def api_reddit_status():
        cfg = repo.get_config()
        token = cfg.get("reddit_access_token")
        connected = bool(token and reddit_oauth.current_token())
        return jsonify({
            "connected": connected,
            "username": cfg.get("reddit_username", "") if connected else "",
            "has_credentials": reddit_oauth.configured,
        })

    @app.route("/api/auth/reddit/disconnect", methods=["POST"])
    def api_reddit_disconnect():
        reddit_oauth.disconnect()
        return jsonify({"ok": True})

    # ----------------------- bulk personalized outreach ---------------------
    # Operator picks N businesses + a template, hits "Draft + queue".
    # We loop, run each through the AI personalizer (when the template uses
    # {personalized_opener}), draft the email, enforce suppression + daily
    # cap + per-domain cap, and enqueue with staggered scheduled_for times.
    # Dashboard UI for this lives in a follow-up PR — invoke via API for now.

    @app.route("/api/outreach/bulk-draft", methods=["POST"])
    def api_outreach_bulk_draft():
        payload = request.get_json(silent=True) or {}
        business_ids = payload.get("business_ids") or []
        if not isinstance(business_ids, list) or not business_ids:
            return jsonify({"ok": False, "error": "business_ids[] required"}), 400

        template_key = payload.get("template_key") or None
        cadence_seconds = int(payload.get("cadence_seconds") or 180)

        start_at = None
        if payload.get("start_at"):
            try:
                raw = payload["start_at"]
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                start_at = parsed
            except (ValueError, AttributeError):
                return jsonify({
                    "ok": False,
                    "error": f"Invalid start_at: {payload.get('start_at')!r}",
                }), 400

        try:
            summary = bulk_draft_and_queue(
                business_ids=business_ids,
                repo=ctx.repo,
                queue=ctx.email_queue,
                suppression=ctx.suppression,
                niche=ctx.niche,
                personalizer=ctx.personalizer,
                template_key=template_key,
                start_at=start_at,
                cadence_seconds=cadence_seconds,
                from_email=(payload.get("from_email") or "").strip(),
                from_name=(payload.get("from_name") or "").strip(),
                reply_to=(payload.get("reply_to") or "").strip(),
            )
        except Exception as e:
            logger.exception("bulk_draft_and_queue failed")
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify(summary)

    @app.route("/api/outreach/personalizer/status")
    def api_personalizer_status():
        """Is the AI personalizer ready? Used by the dashboard to gate the
        bulk button + show the model in use."""
        return jsonify({
            "configured": ctx.personalizer.configured,
            "model": ctx.personalizer.model,
            "cache_size": len(ctx.personalizer._cache),
        })

    # ----------------------- send-volume limits -----------------------------

    @app.route("/api/emails/send-limits")
    def api_send_limits():
        """Current daily cap, per-domain cap, warm-up status, today's usage."""
        from cwscraper.email.send_limits import todays_counts
        summary = send_limits_summary()
        counts = todays_counts(ctx.email_queue)
        summary["today_total"] = counts["total"]
        summary["today_by_domain"] = counts["by_domain"]
        return jsonify(summary)

    # ----------------------- inbound replies --------------------------------
    # Background poller drives this; the routes below are for the dashboard
    # ops panel (status, manual poll, force-recheck).

    @app.route("/api/emails/inbound/status")
    def api_inbound_status():
        return jsonify(inbound_settings_summary())

    @app.route("/api/emails/inbound/poll-now", methods=["POST"])
    def api_inbound_poll_now():
        """Synchronous manual poll. Useful for first-time wiring + debugging."""
        return jsonify(ctx.inbound_poller.tick())

    # ----------------------- inbound auto-reply drafts ----------------------
    # When the IMAP poller classifies a reply as 'interested', it auto-drafts
    # a second-touch response (via cwscraper.replies.auto_reply) and queues
    # it here for operator review. Operator approves (moves to scheduled-
    # emails queue) or dismisses.

    @app.route("/api/replies/inbound-drafts")
    def api_inbound_drafts_list():
        status = request.args.get("status")  # optional: pending|approved|dismissed
        drafts = ctx.inbound_drafts.list(status=status)
        return jsonify({
            "ok": True,
            "drafts": drafts,
            "pending_count": sum(1 for d in drafts if d.get("status") == "pending"),
        })

    @app.route("/api/replies/inbound-drafts/<draft_id>/edit", methods=["POST"])
    def api_inbound_draft_edit(draft_id):
        """Operator edits the AI-drafted subject/body before approving."""
        payload = request.get_json(silent=True) or {}
        updated = ctx.inbound_drafts.update_body(
            draft_id,
            subject=payload.get("subject"),
            body=payload.get("body"),
        )
        if not updated:
            return jsonify({"ok": False, "error": "draft not found"}), 404
        if updated.get("status") != "pending":
            return jsonify({"ok": False, "error": "draft already decided"}), 400
        return jsonify({"ok": True, "draft": updated})

    @app.route("/api/replies/inbound-drafts/<draft_id>/approve", methods=["POST"])
    def api_inbound_draft_approve(draft_id):
        """Approve a draft → enqueue it for send via the existing scheduled-
        emails queue. Default schedule: 5 minutes from now (gives the
        operator a brief window to revoke before SMTP fires)."""
        draft = ctx.inbound_drafts.get(draft_id)
        if not draft:
            return jsonify({"ok": False, "error": "draft not found"}), 404
        if draft.get("status") != "pending":
            return jsonify({"ok": False, "error": "draft already decided"}), 400
        if not (draft.get("to_email") or "").strip():
            return jsonify({"ok": False, "error": "draft has no recipient email"}), 400

        # 5 minutes from now — gives the operator a window to revoke
        from datetime import timedelta
        scheduled_for = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

        entry = ctx.email_queue.enqueue(
            prospect_id=draft.get("business_id", ""),
            lead_type="business",
            to_email=draft["to_email"],
            subject=draft["subject"],
            body=draft["body"],
            scheduled_for=scheduled_for,
        )
        ctx.inbound_drafts.mark_approved(draft_id, queue_id=entry["id"])
        return jsonify({
            "ok": True,
            "queue_id": entry["id"],
            "scheduled_for": scheduled_for,
        })

    @app.route("/api/replies/inbound-drafts/<draft_id>/dismiss", methods=["POST"])
    def api_inbound_draft_dismiss(draft_id):
        """Dismiss a draft without sending. The prospect's stage is
        unchanged — they're still in reply_received, the operator just
        decided not to use this AI-drafted response."""
        draft = ctx.inbound_drafts.get(draft_id)
        if not draft:
            return jsonify({"ok": False, "error": "draft not found"}), 404
        if draft.get("status") != "pending":
            return jsonify({"ok": False, "error": "draft already decided"}), 400
        ctx.inbound_drafts.mark_dismissed(draft_id)
        return jsonify({"ok": True})

    # ----------------------- suppression list -------------------------------
    # Backs the unsubscribe flow + the operator-facing do-not-contact panel.

    @app.route("/api/suppression")
    def api_suppression_list():
        try:
            limit = min(int(request.args.get("limit", 500)), 5000)
        except ValueError:
            limit = 500
        return jsonify({
            "ok": True,
            "items": ctx.suppression.list_all(limit=limit),
        })

    @app.route("/api/suppression", methods=["POST"])
    def api_suppression_add():
        payload = request.get_json(silent=True) or {}
        email_addr = (payload.get("email") or "").strip()
        reason = (payload.get("reason") or "manual").strip()
        notes = (payload.get("notes") or "").strip()
        entry = ctx.suppression.add(email_addr, reason=reason, notes=notes, added_by="dashboard")
        if entry is None:
            return jsonify({"ok": False, "error": "invalid email"}), 400
        return jsonify({"ok": True, "entry": entry})

    @app.route("/api/suppression/<path:email_addr>", methods=["DELETE"])
    def api_suppression_remove(email_addr):
        removed = ctx.suppression.remove(email_addr)
        return jsonify({"ok": removed})

    # ----------------------- public unsubscribe -----------------------------
    # Backs the List-Unsubscribe header + the link in the email footer.
    # We never reveal whether a given address was on our list, so the
    # response is the same for unknown addresses.

    def _do_unsubscribe(addr: str, source: str) -> bool:
        target = (addr or "").strip().lower()
        if not target or "@" not in target:
            return False
        ctx.suppression.add(
            target, reason="unsubscribe",
            notes=f"via public link ({source})",
            added_by="unsubscribe-link",
        )
        # If this address matches a known business prospect, mark it lost.
        for p in repo.get_all_prospects():
            if p.get("lead_type") != "business":
                continue
            emails = [(p.get("email") or "").strip().lower()]
            for c in p.get("contacts") or []:
                emails.append((c.get("email") or "").strip().lower())
            if target in emails:
                repo.update_prospect(
                    p["id"], "business",
                    {"pipeline_stage": "lost"},
                    action="unsubscribed_via_link",
                )
        # Mirror the opt-out to the CRM so it flips do_not_email + stops any drip.
        # Without this, a public/one-click unsubscribe only lands in the scraper's
        # suppression list and the CRM keeps re-enrolling the address.
        try:
            from cwscraper.integrations.crm import notify_email_reply
            notify_email_reply(
                from_email=target,
                classification="unsubscribe",
                subject="Unsubscribe",
                snippet=f"Unsubscribed via public link ({source})",
            )
        except Exception:  # noqa: BLE001 — best-effort, never fail the unsubscribe
            pass
        return True

    @app.route("/unsubscribe", methods=["GET"])
    def unsubscribe_get():
        addr = request.args.get("email", "")
        accepted = _do_unsubscribe(addr, source="GET")
        return render_template("unsubscribe.html", email=addr, accepted=accepted), 200

    @app.route("/unsubscribe", methods=["POST"])
    def unsubscribe_one_click():
        """RFC 8058 one-click — mail clients (Gmail, Outlook) POST here."""
        payload = request.get_json(silent=True) or {}
        addr = (
            request.form.get("email")
            or request.args.get("email")
            or payload.get("email", "")
        )
        _do_unsubscribe(addr, source="one-click")
        return jsonify({"ok": True}), 200

    return app


def _back_compat_leads(leads: list[dict]) -> list[dict]:
    """Old dashboard expects `subreddit` field; new model uses `source`.

    Map source -> subreddit so the existing HTML works unmodified.
    """
    for l in leads:
        if "subreddit" not in l:
            src = l.get("source", "")
            # 'r/AgingParents' -> 'AgingParents'
            l["subreddit"] = src[2:] if src.startswith("r/") else src
    return leads


# WSGI entrypoint
app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("CWSCRAPER_PORT", "5050"))
    host = os.getenv("CWSCRAPER_HOST", "0.0.0.0")
    cwscraper_ctx = app.extensions["cwscraper"]
    print(f"\n  CheckWell Enterprise Scraper v{__version__}")
    print(f"  Niche pack: {cwscraper_ctx.niche.display_name} ({cwscraper_ctx.niche.mode} mode)")
    print(f"  Dashboard:  http://localhost:{port}\n")
    app.run(host=host, port=port, debug=True)
