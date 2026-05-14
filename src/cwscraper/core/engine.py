"""Scan engine — runs every enabled scanner, sorts, persists results.

Dispatches based on niche.mode:
  - community → scans Reddit/YouTube/HN, produces Lead rows
  - directory → scans Google Places (etc.), produces BusinessLead rows
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from cwscraper.core.models import Lead
from cwscraper.core.niche import NichePack
from cwscraper.core.store import Repository
from cwscraper.scanners import ALL_DIRECTORY_SCANNERS, ALL_SCANNERS
from cwscraper.scanners.base import ScannerContext
from cwscraper.scanners.directory_base import DirectoryContext


class ScanEngine:
    """Orchestrates scanners + persistence. Thread-safe scan locking.

    A single engine instance serves both community and directory modes; the
    run_full_scan path branches on self.niche.mode.
    """

    def __init__(self, repo: Repository, niche: NichePack):
        self.repo = repo
        self.niche = niche
        self._lock = threading.Lock()
        self.is_scanning = False
        self.last_scan: dict | None = None
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
