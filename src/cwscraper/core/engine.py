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

ENRICHMENT_WORKERS = 5


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

        start = time.time()
        try:
            return self._do_enrichment(enricher_slug, only_missing_email, limit, start)
        finally:
            self.is_enriching = False
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

        def _do_one(biz: dict) -> tuple[str, object]:
            try:
                result = enricher.enrich(biz, ctx)
            except Exception as e:
                ctx.log_error(biz.get("name", biz.get("id", "?")), f"unhandled: {e}")
                result = None
            return biz.get("id", ""), result

        with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as pool:
            futures = [pool.submit(_do_one, biz) for biz in candidates]
            for fut in as_completed(futures):
                biz_id, result = fut.result()
                if result and (result.email or result.contacts):
                    patch = {}
                    if result.email:
                        patch["email"] = result.email
                    if result.contacts:
                        patch["contacts"] = result.contacts
                    if patch:
                        self.repo.update_business(biz_id, patch)
                with progress_lock:
                    ctx.businesses_done += 1
                    if result and result.email:
                        ctx.emails_found += 1
                    self.enrichment_progress.update(
                        businesses_done=ctx.businesses_done,
                        emails_found=ctx.emails_found,
                        elapsed_seconds=round(time.time() - start, 1),
                        errors=list(ctx.errors)[-20:],  # last 20 only
                    )

        elapsed = round(time.time() - start, 1)
        result_summary = {
            "enricher": enricher_slug,
            "businesses_total": ctx.businesses_total,
            "businesses_done": ctx.businesses_done,
            "emails_found": ctx.emails_found,
            "duration_seconds": elapsed,
            "errors": list(ctx.errors)[-20:],
        }
        self.last_enrichment = result_summary
        self.enrichment_progress.update(status="complete", elapsed_seconds=elapsed)
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

        elapsed = round(time.time() - start, 1)
        result = {
            "mode": "directory",
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
