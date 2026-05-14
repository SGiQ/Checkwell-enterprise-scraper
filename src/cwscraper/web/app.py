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
from cwscraper.core.niche import list_bundled_niches, load_niche
from cwscraper.core.scheduler import AutoScanner
from cwscraper.core.store import JSONRepository
from cwscraper.replies import RedditOAuth, draft_outreach, draft_reply, post_reddit_comment

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
        threading.Thread(target=ctx.engine.run_full_scan, daemon=True).start()
        return jsonify({"status": "started"})

    @app.route("/api/scan/status")
    def api_scan_status():
        return jsonify({
            "is_scanning": ctx.engine.is_scanning,
            "last_scan": ctx.engine.last_scan,
            "progress": ctx.engine.progress,
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
        has_website = request.args.get("has_website")
        min_rating = request.args.get("min_rating", type=float)
        search = request.args.get("search", "").lower()

        if status:
            businesses = [b for b in businesses if b.get("status") == status]
        if state:
            businesses = [b for b in businesses if b.get("state") == state]
        if city:
            businesses = [b for b in businesses if b.get("city", "").lower() == city.lower()]
        if has_website in ("true", "1"):
            businesses = [b for b in businesses if b.get("website")]
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
        """Append/update contact info on a business lead.

        Body: {email?: str, contacts?: [{name, title, email, phone}]}
        """
        data = request.get_json() or {}
        patch = {}
        if "email" in data:
            patch["email"] = data["email"]
        if "contacts" in data:
            patch["contacts"] = data["contacts"]
        if not patch:
            return jsonify({"error": "Provide email and/or contacts"}), 400
        repo.update_business(business_id, patch)
        return jsonify({"ok": True})

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
        return jsonify(draft_outreach(business, ctx.niche, template_key))

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
