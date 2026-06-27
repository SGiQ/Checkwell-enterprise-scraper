"""Scheduled-email queue, persisted as JSON next to the other repo files.

A scheduled email entry:
    {
      "id": "uuid",
      "prospect_id": "biz-abc",
      "lead_type": "business",     # 'business' or 'community'
      "to_email": "owner@example.com",
      "subject": "...",
      "body": "...",
      "from_email": "shaun@checkwellcall.com",
      "from_name":  "Shaun (CheckWellCall)",
      "reply_to":   "shaun@checkwellcall.com",  # optional
      "scheduled_for": "2026-05-20T14:00:00Z",  # ISO UTC
      "status": "pending",                       # pending|sent|failed|cancelled
      "created_at": "2026-05-16T...",
      "sent_at": "",                              # set when sent
      "provider_id": "",                          # transport's message id
      "error": "",                                # set when failed
    }
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScheduledEmailQueue:
    """File-backed queue. Reads/writes data/scheduled_emails.json."""

    def __init__(self, data_dir: Path):
        self.file = data_dir / "scheduled_emails.json"
        self._lock = threading.RLock()

    # --- read paths ---

    def _all(self) -> list[dict]:
        if not self.file.exists():
            return []
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def list(self, status: str | None = None, prospect_id: str | None = None) -> list[dict]:
        rows = self._all()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if prospect_id:
            rows = [r for r in rows if r.get("prospect_id") == prospect_id]
        # Newest-scheduled-first, then pending before terminal
        rows.sort(key=lambda r: (r.get("status") != "pending", r.get("scheduled_for", "")))
        return rows

    def get(self, email_id: str) -> dict | None:
        for r in self._all():
            if r.get("id") == email_id:
                return r
        return None

    def due_pending(self, now_iso: str | None = None) -> list[dict]:
        """Pending emails whose scheduled_for has passed."""
        cutoff = now_iso or _utcnow_iso()
        return [
            r for r in self._all()
            if r.get("status") == "pending"
            and r.get("scheduled_for", "") <= cutoff
        ]

    # --- write paths ---

    def enqueue(
        self,
        *,
        prospect_id: str,
        lead_type: str,
        to_email: str,
        subject: str,
        body: str,
        scheduled_for: str,
        body_html: str = "",
        from_email: str = "",
        from_name: str = "",
        reply_to: str = "",
    ) -> dict:
        entry = {
            "id": uuid.uuid4().hex,
            "prospect_id": prospect_id,
            "lead_type": lead_type,
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "body_html": body_html,
            "from_email": from_email,
            "from_name": from_name,
            "reply_to": reply_to,
            "scheduled_for": scheduled_for,
            "status": "pending",
            "created_at": _utcnow_iso(),
            "sent_at": "",
            "provider_id": "",
            "error": "",
        }
        with self._lock:
            rows = self._all()
            rows.append(entry)
            self._write(rows)
        return entry

    def mark_sent(self, email_id: str, provider_id: str = "") -> dict | None:
        return self._patch(email_id, {
            "status": "sent",
            "sent_at": _utcnow_iso(),
            "provider_id": provider_id,
            "error": "",
        })

    def mark_failed(self, email_id: str, error: str) -> dict | None:
        return self._patch(email_id, {
            "status": "failed",
            "error": (error or "")[:300],
        })

    def cancel(self, email_id: str) -> dict | None:
        existing = self.get(email_id)
        if not existing or existing.get("status") != "pending":
            return None
        return self._patch(email_id, {"status": "cancelled"})

    # --- internals ---

    def _patch(self, email_id: str, patch: dict) -> dict | None:
        with self._lock:
            rows = self._all()
            updated = None
            for r in rows:
                if r.get("id") == email_id:
                    r.update(patch)
                    updated = r
                    break
            if updated is not None:
                self._write(rows)
            return updated

    def _write(self, rows: list[dict]) -> None:
        self.file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
