"""Storage layer.

Defines a Repository protocol so Phase 2 can swap JSON files for
SQLAlchemy + Postgres without touching scanner or web code.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

from cwscraper.core.models import (
    LEGACY_STATUS_TO_STAGE,
    PIPELINE_STAGES,
    BusinessLead,
    Lead,
)


def _ensure_pipeline_fields(row: dict) -> None:
    """Backfill new pipeline fields on rows from older versions of the file.

    Maps legacy `status` (new/reviewed/qualified/contacted/dismissed) onto
    the new `pipeline_stage` if the row hasn't been touched yet.
    """
    if "pipeline_stage" not in row or not row.get("pipeline_stage"):
        legacy = (row.get("status") or "new").lower()
        row["pipeline_stage"] = LEGACY_STATUS_TO_STAGE.get(legacy, "new")
    row.setdefault("notes", "")
    row.setdefault("follow_up_date", "")
    row.setdefault("tags", [])
    row.setdefault("activity_log", [])


def _diff_summary(row: dict, patch: dict) -> str:
    """Produce a short human-readable description of what changed."""
    parts: list[str] = []
    for key, new_val in patch.items():
        old_val = row.get(key)
        if old_val == new_val:
            continue
        if key == "pipeline_stage":
            parts.append(f"{old_val or 'new'} -> {new_val}")
        elif key == "tags":
            parts.append(f"tags={new_val}")
        elif key == "follow_up_date":
            parts.append(f"follow_up={new_val or 'cleared'}")
        elif key == "notes":
            parts.append("notes updated")
        else:
            parts.append(f"{key}={new_val}")
    return "; ".join(parts) if parts else "no-op"


def _data_dir() -> Path:
    """Resolve the data directory.

    Defaults to ./data (cwd) so a fresh install just works. Override
    with CWSCRAPER_DATA_DIR for Docker volumes or shared storage.
    """
    raw = os.getenv("CWSCRAPER_DATA_DIR", "./data")
    path = Path(raw).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


class Repository(Protocol):
    """Storage interface — scanners and web layer only see this.

    The JSON implementation below is fine for single-tenant. Phase 2
    will add a SqlAlchemyRepository for multi-tenant Postgres.
    """

    # --- community-mode leads (Reddit/YouTube/HN posts) ---
    def get_leads(self) -> list[dict]: ...
    def add_leads(self, leads: Iterable[Lead]) -> None: ...
    def update_lead_status(self, lead_id: str, status: str) -> None: ...

    # --- directory-mode businesses (Google Places, Yelp) ---
    def get_businesses(self) -> list[dict]: ...
    def add_businesses(self, businesses: Iterable[BusinessLead]) -> None: ...
    def update_business_status(self, business_id: str, status: str) -> None: ...
    def update_business(self, business_id: str, patch: dict) -> None: ...

    # --- pipeline (CRM-lite, unified across leads + businesses) ---
    def get_all_prospects(self) -> list[dict]: ...
    def update_prospect(
        self, prospect_id: str, lead_type: str, patch: dict, action: str = ""
    ) -> dict | None: ...

    def get_seen_ids(self) -> set[str]: ...
    def save_seen_ids(self, new_ids: dict[str, float]) -> None: ...

    def get_config(self) -> dict: ...
    def save_config(self, config: dict) -> None: ...

    def log_scan(self, result: dict) -> None: ...
    def get_scan_logs(self) -> list[dict]: ...

    def get_replies(self) -> list[dict]: ...
    def save_reply(self, reply: dict) -> None: ...
    def update_reply_status(self, lead_id: str, status: str) -> None: ...


class JSONRepository:
    """File-based repo. Sufficient for single-tenant installs."""

    def __init__(self, data_dir: Path | None = None):
        self.dir = data_dir or _data_dir()
        self.leads_file = self.dir / "leads.json"
        self.businesses_file = self.dir / "businesses.json"
        self.seen_file = self.dir / "seen_ids.json"
        self.config_file = self.dir / "config.json"
        self.scan_log_file = self.dir / "scan_log.json"
        self.replies_file = self.dir / "replies.json"
        self._lock = threading.RLock()

    # --- leads ---
    def get_leads(self) -> list[dict]:
        with self._lock:
            if self.leads_file.exists():
                return json.loads(self.leads_file.read_text(encoding="utf-8"))
            return []

    def add_leads(self, leads: Iterable[Lead]) -> None:
        with self._lock:
            existing = self.get_leads()
            existing.extend(asdict(lead) for lead in leads)
            self.leads_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def update_lead_status(self, lead_id: str, status: str) -> None:
        with self._lock:
            leads = self.get_leads()
            for lead in leads:
                if lead["id"] == lead_id:
                    lead["status"] = status
                    break
            self.leads_file.write_text(json.dumps(leads, indent=2), encoding="utf-8")

    # --- businesses (directory mode) ---
    def get_businesses(self) -> list[dict]:
        with self._lock:
            if self.businesses_file.exists():
                return json.loads(self.businesses_file.read_text(encoding="utf-8"))
            return []

    def add_businesses(self, businesses: Iterable[BusinessLead]) -> None:
        with self._lock:
            existing = self.get_businesses()
            by_id = {b["id"]: b for b in existing}
            for biz in businesses:
                row = asdict(biz)
                if row["id"] in by_id:
                    # Merge: keep status / contacts / email from existing,
                    # refresh everything else from latest scan.
                    prior = by_id[row["id"]]
                    row["status"] = prior.get("status", "new")
                    row["email"] = row["email"] or prior.get("email", "")
                    row["contacts"] = row["contacts"] or prior.get("contacts", [])
                by_id[row["id"]] = row
            self.businesses_file.write_text(
                json.dumps(list(by_id.values()), indent=2), encoding="utf-8"
            )

    def update_business_status(self, business_id: str, status: str) -> None:
        self.update_business(business_id, {"status": status})

    def update_business(self, business_id: str, patch: dict) -> None:
        with self._lock:
            businesses = self.get_businesses()
            for biz in businesses:
                if biz["id"] == business_id:
                    biz.update(patch)
                    break
            self.businesses_file.write_text(
                json.dumps(businesses, indent=2), encoding="utf-8"
            )

    # --- pipeline / CRM-lite (unified across leads + businesses) ---

    def get_all_prospects(self) -> list[dict]:
        """Return both lead types in one flat list, tagged with lead_type."""
        leads = self.get_leads()
        businesses = self.get_businesses()
        prospects: list[dict] = []
        for row in leads:
            _ensure_pipeline_fields(row)
            prospects.append({**row, "lead_type": "community"})
        for row in businesses:
            _ensure_pipeline_fields(row)
            prospects.append({**row, "lead_type": "business"})
        return prospects

    def update_prospect(
        self,
        prospect_id: str,
        lead_type: str,
        patch: dict,
        action: str = "",
    ) -> dict | None:
        """Patch a community lead or business lead. Appends to activity_log."""
        with self._lock:
            file_path = self.leads_file if lead_type == "community" else self.businesses_file
            if not file_path.exists():
                return None
            rows = json.loads(file_path.read_text(encoding="utf-8"))
            updated: dict | None = None
            for row in rows:
                if row.get("id") != prospect_id:
                    continue
                _ensure_pipeline_fields(row)
                if action:
                    detail = _diff_summary(row, patch)
                    row["activity_log"] = (row.get("activity_log") or []) + [{
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "action": action,
                        "detail": detail,
                    }]
                row.update(patch)
                updated = row
                break
            if updated is not None:
                file_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
            return updated

    # --- seen IDs (30-day rolling window) ---
    def get_seen_ids(self) -> set[str]:
        with self._lock:
            if not self.seen_file.exists():
                return set()
            try:
                data = json.loads(self.seen_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError):
                return set()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            return {pid for pid, ts in data.items() if ts > cutoff}

    def save_seen_ids(self, new_ids: dict[str, float]) -> None:
        with self._lock:
            existing: dict[str, float] = {}
            if self.seen_file.exists():
                try:
                    existing = json.loads(self.seen_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, TypeError):
                    pass
            existing.update(new_ids)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            pruned = {pid: ts for pid, ts in existing.items() if ts > cutoff}
            self.seen_file.write_text(json.dumps(pruned), encoding="utf-8")

    # --- config (per-install settings) ---
    _DEFAULT_CONFIG = {
        "scan_interval_hours": 6,
        "auto_scan_enabled": False,
        "youtube_enabled": True,
        "hackernews_enabled": True,
        "reddit_enabled": True,
    }

    def get_config(self) -> dict:
        with self._lock:
            if self.config_file.exists():
                try:
                    saved = json.loads(self.config_file.read_text(encoding="utf-8"))
                    return {**self._DEFAULT_CONFIG, **saved}
                except (json.JSONDecodeError, TypeError):
                    pass
            self.config_file.write_text(json.dumps(self._DEFAULT_CONFIG, indent=2), encoding="utf-8")
            return dict(self._DEFAULT_CONFIG)

    def save_config(self, config: dict) -> None:
        with self._lock:
            self.config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # --- scan log (last 100 runs) ---
    def log_scan(self, result: dict) -> None:
        with self._lock:
            logs = self.get_scan_logs()
            logs.insert(0, result)
            self.scan_log_file.write_text(json.dumps(logs[:100], indent=2), encoding="utf-8")

    def get_scan_logs(self) -> list[dict]:
        with self._lock:
            if not self.scan_log_file.exists():
                return []
            try:
                return json.loads(self.scan_log_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError):
                return []

    # --- replies ---
    def get_replies(self) -> list[dict]:
        with self._lock:
            if not self.replies_file.exists():
                return []
            return json.loads(self.replies_file.read_text(encoding="utf-8"))

    def save_reply(self, reply: dict) -> None:
        with self._lock:
            existing = self.get_replies()
            reply.setdefault("status", "draft")
            reply.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            existing = [r for r in existing if r.get("lead_id") != reply.get("lead_id")]
            existing.insert(0, reply)
            self.replies_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def update_reply_status(self, lead_id: str, status: str) -> None:
        with self._lock:
            replies = self.get_replies()
            for r in replies:
                if r.get("lead_id") == lead_id:
                    r["status"] = status
                    if status == "sent":
                        r["sent_at"] = datetime.now(timezone.utc).isoformat()
                    break
            self.replies_file.write_text(json.dumps(replies, indent=2), encoding="utf-8")
