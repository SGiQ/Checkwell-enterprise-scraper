"""Scan engine — runs every enabled scanner, sorts, persists results.

Dispatches based on niche.mode:
  - community → scans Reddit/YouTube/HN, produces Lead rows
  - directory → scans Google Places (etc.), produces BusinessLead rows
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from cwscraper.core.models import Lead
from cwscraper.core.niche import NichePack
from cwscraper.core.store import Repository
from cwscraper.enrichment import ALL_ENRICHERS
from cwscraper.enrichment.base import EnrichmentContext
from cwscraper.scanners import ALL_DIRECTORY_SCANNERS, ALL_SCANNERS
from cwscraper.scanners.base import ScannerContext
from cwscraper.scanners.directory_base import DirectoryContext

import os

# Default thread pool size for enrichment. Heavy enrichers (Playwright)
# override via `suggested_workers` on the class. Override globally with
# CWSCRAPER_ENRICHMENT_WORKERS for low-memory hosts.
DEFAULT_ENRICHMENT_WORKERS = int(os.getenv("CWSCRAPER_ENRICHMENT_WORKERS", "5"))


class ScanEngine:
    """Orchestrates scanners + persistence. Thread-safe scan locking.

    A single engine instance serves both community and directory modes; the
    run_full_scan path branches on self.niche.mode.
    """

    def __init__(self, repo: Repository, niche: NichePack):
        self.repo = repo
        self.niche = niche
        self._lock = threading.Lock()
        self._enrich_lock = threading.Lock()
        self._enrich_cancel = threading.Event()   # set => current run should bail out
        self.is_scanning = False
        self.is_enriching = False
        self.last_scan: dict | None = None
        self.last_enrichment: dict | None = None
        self.progress: dict = {
            "status": "idle",
            "mode": niche.mode,
            "current_platform": "",
            "platforms_done": 0,
            "platforms_total": 0,
            "leads_found_so_far": 0,
            "elapsed_seconds": 0,
            "errors": [],
        }
        self.enrichment_progress: dict = {
            "status": "idle",
            "businesses_total": 0,
            "businesses_done": 0,
            "emails_found": 0,
            "elapsed_seconds": 0,
            "errors": [],
        }

    def cancel_enrichment(self) -> bool:
        """Signal the running enrichment loop to stop after the current item.

        Returns True if a run was in progress (cancellation requested),
        False if nothing was running.
        """
        if not self.is_enriching:
            return False
        self._enrich_cancel.set()
        self.enrichment_progress["status"] = "cancelling"
        return True

    def run_full_scan(self) -> dict:
        if self.is_scanning:
            return {"error": "Scan already in progress"}

        with self._lock:
            if self.is_scanning:
                return {"error": "Scan already in progress"}
            self.is_scanning = True

        start = time.time()
        try:
            if self.niche.mode == "directory":
                return self._do_directory_scan(start)
            return self._do_scan(start)
        finally:
            self.is_scanning = False
            self.progress["status"] = "idle"

    def _enabled_scanners(self) -> list[str]:
        config = self.repo.get_config()
        enabled = []
        for slug in ALL_SCANNERS:
            if config.get(f"{slug}_enabled", True):
                enabled.append(slug)
        return enabled

    def _do_scan(self, start: float) -> dict:
        scanner_slugs = self._enabled_scanners()
        seen_ids = self.repo.get_seen_ids()
        ctx = ScannerContext(seen_ids=set(seen_ids))

        self.progress.update(
            status="scanning",
            platforms_total=len(scanner_slugs),
            platforms_done=0,
            leads_found_so_far=0,
            errors=[],
        )

        all_leads: list[Lead] = []
        all_new_seen: dict[str, float] = {}
        by_source: dict[str, dict] = {}

        for idx, slug in enumerate(scanner_slugs):
            cls = ALL_SCANNERS[slug]
            scanner = cls(self.niche)
            self.progress.update(
                current_platform=scanner.name,
                elapsed_seconds=round(time.time() - start, 1),
            )
            try:
                leads, new_seen = scanner.scan(ctx)
            except Exception as e:
                ctx.log_error(scanner.name, f"Unhandled scanner exception: {e}")
                leads, new_seen = [], {}

            all_leads.extend(leads)
            all_new_seen.update(new_seen)
            ctx.seen_ids.update(new_seen.keys())
            by_source[slug] = {"posts_checked": len(new_seen), "leads_found": len(leads)}

            self.progress.update(
                platforms_done=idx + 1,
                leads_found_so_far=len(all_leads),
                elapsed_seconds=round(time.time() - start, 1),
                errors=list(ctx.errors),
            )

        all_leads.sort(
            key=lambda l: (l.intent_level != "high", -(l.score + l.num_comments * 2))
        )

        if all_leads:
            self.repo.add_leads(all_leads)
        if all_new_seen:
            self.repo.save_seen_ids(all_new_seen)

        elapsed = round(time.time() - start, 1)
        result = {
            "kind": "scan",
            "mode": "community",
            "niche": self.niche.slug,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": elapsed,
            "sources_scanned": len(scanner_slugs),
            "posts_checked": len(all_new_seen),
            "leads_found": len(all_leads),
            "high_intent": sum(1 for l in all_leads if l.intent_level == "high"),
            "medium_intent": sum(1 for l in all_leads if l.intent_level == "medium"),
            "by_source": by_source,
            "errors": list(ctx.errors),
        }
        self.repo.log_scan(result)
        self.last_scan = result
        self.progress.update(status="complete", current_platform="Done")
        return result

    # ----------------------- enrichment ---------------------------------

    def run_enrichment(
        self,
        enricher_slug: str = "website",
        only_missing_email: bool = True,
        limit: int | None = None,
    ) -> dict:
        """Enrich BusinessLeads with contact info.

        only_missing_email=True (default): skip businesses that already have an email
        limit: cap how many to process (None = all)
        """
        if self.is_enriching:
            return {"error": "Enrichment already in progress"}
        with self._enrich_lock:
            if self.is_enriching:
                return {"error": "Enrichment already in progress"}
            self.is_enriching = True
            self._enrich_cancel.clear()

        start = time.time()
        try:
            return self._do_enrichment(enricher_slug, only_missing_email, limit, start)
        finally:
            self.is_enriching = False
            # Preserve the 'cancelled' status if the loop bailed out — otherwise idle
            if self.enrichment_progress.get("status") != "cancelled":
                self.enrichment_progress["status"] = "idle"

    def _do_enrichment(
        self,
        enricher_slug: str,
        only_missing_email: bool,
        limit: int | None,
        start: float,
    ) -> dict:
        if enricher_slug not in ALL_ENRICHERS:
            return {"error": f"Unknown enricher: {enricher_slug}"}
        enricher = ALL_ENRICHERS[enricher_slug]()

        all_biz = self.repo.get_businesses()
        candidates = [
            b for b in all_biz
            if b.get("website") and (not only_missing_email or not b.get("email"))
        ]
        if limit is not None:
            candidates = candidates[:limit]

        ctx = EnrichmentContext(businesses_total=len(candidates))
        self.enrichment_progress.update(
            status="running",
            businesses_total=len(candidates),
            businesses_done=0,
            emails_found=0,
            elapsed_seconds=0,
            errors=[],
        )

        if not candidates:
            self.enrichment_progress.update(status="complete", elapsed_seconds=0)
            return {
                "enricher": enricher_slug,
                "businesses_total": 0,
                "businesses_done": 0,
                "emails_found": 0,
                "duration_seconds": 0,
                "errors": [],
            }

        progress_lock = threading.Lock()
        cancelled = False
        enriched_ids: set[str] = set()

        def _do_one(biz: dict) -> tuple[str, object, bool]:
            # Honor cancel signal — drop the work without running the enricher.
            if self._enrich_cancel.is_set():
                return biz.get("id", ""), None, True
            try:
                result = enricher.enrich(biz, ctx)
            except Exception as e:
                ctx.log_error(biz.get("name", biz.get("id", "?")), f"unhandled: {e}")
                result = None
            return biz.get("id", ""), result, False

        # Heavy enrichers (Playwright) request lower concurrency to keep memory in check.
        suggested = getattr(enricher, "suggested_workers", None)
        max_workers = min(suggested or DEFAULT_ENRICHMENT_WORKERS, DEFAULT_ENRICHMENT_WORKERS)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_do_one, biz) for biz in candidates]
            for fut in as_completed(futures):
                biz_id, result, skipped = fut.result()
                if skipped:
                    cancelled = True
                if result and (result.email or result.contacts):
                    patch = {}
                    if result.email:
                        patch["email"] = result.email
                    if result.contacts:
                        patch["contacts"] = result.contacts
                    if patch:
                        self.repo.update_business(biz_id, patch)
                        enriched_ids.add(biz_id)
                with progress_lock:
                    if not skipped:
                        ctx.businesses_done += 1
                        if result and result.email:
                            ctx.emails_found += 1
                    self.enrichment_progress.update(
                        businesses_done=ctx.businesses_done,
                        emails_found=ctx.emails_found,
                        elapsed_seconds=round(time.time() - start, 1),
                        errors=list(ctx.errors)[-20:],  # last 20 only
                    )

        # Push freshly-enriched businesses (now carrying email/contacts) to the
        # CRM so it gets the contact info. Idempotent via external_id (merges).
        if enriched_ids:
            try:
                from cwscraper.integrations.crm import push_businesses
                enriched = [b for b in self.repo.get_businesses() if b.get("id") in enriched_ids]
                push_businesses(enriched)
            except Exception:
                pass

        elapsed = round(time.time() - start, 1)
        final_status = "cancelled" if cancelled else "complete"
        result_summary = {
            "kind": "enrichment",
            "niche": self.niche.slug,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "enricher": enricher_slug,
            "status": final_status,
            "businesses_total": ctx.businesses_total,
            "businesses_done": ctx.businesses_done,
            "emails_found": ctx.emails_found,
            "duration_seconds": elapsed,
            "errors": list(ctx.errors)[-20:],
        }
        self.last_enrichment = result_summary
        # Persist to scan history so the dashboard's Scan History tab shows
        # enrichment runs alongside discovery scans.
        self.repo.log_scan(result_summary)
        self.enrichment_progress.update(status=final_status, elapsed_seconds=elapsed)
        return result_summary

    # ----------------------- directory mode -----------------------------

    def _enabled_directory_scanners(self) -> list[str]:
        config = self.repo.get_config()
        return [
            slug for slug in ALL_DIRECTORY_SCANNERS
            if config.get(f"{slug}_enabled", True)
        ]

    def _do_directory_scan(self, start: float) -> dict:
        scanner_slugs = self._enabled_directory_scanners()
        ctx = DirectoryContext()

        self.progress.update(
            status="scanning",
            mode="directory",
            platforms_total=len(scanner_slugs),
            platforms_done=0,
            leads_found_so_far=0,
            errors=[],
        )

        all_biz = []
        by_source: dict[str, dict] = {}
        for idx, slug in enumerate(scanner_slugs):
            cls = ALL_DIRECTORY_SCANNERS[slug]
            scanner = cls(self.niche)
            self.progress.update(
                current_platform=scanner.name,
                elapsed_seconds=round(time.time() - start, 1),
            )
            try:
                businesses = scanner.scan(ctx)
            except Exception as e:
                ctx.log_error(scanner.name, f"Unhandled exception: {e}")
                businesses = []
            all_biz.extend(businesses)
            by_source[slug] = {
                "queries_run": ctx.queries_run,
                "businesses_found": len(businesses),
            }
            self.progress.update(
                platforms_done=idx + 1,
                leads_found_so_far=len(all_biz),
                elapsed_seconds=round(time.time() - start, 1),
                errors=list(ctx.errors),
            )

        if all_biz:
            self.repo.add_businesses(all_biz)
            # Best-effort push to the SGiQ CRM (no-op unless SGIQ_CRM_* env set).
            try:
                from cwscraper.integrations.crm import push_businesses
                push_businesses(all_biz)
            except Exception:  # never let CRM sync break a scan
                pass

        elapsed = round(time.time() - start, 1)
        result = {
            "kind": "scan",
            "mode": "directory",
            "niche": self.niche.slug,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": elapsed,
            "sources_scanned": len(scanner_slugs),
            "queries_run": ctx.queries_run,
            "businesses_found": len(all_biz),
            "by_source": by_source,
            "errors": list(ctx.errors),
        }
        self.repo.log_scan(result)
        self.last_scan = result
        self.progress.update(status="complete", current_platform="Done")
        return result
