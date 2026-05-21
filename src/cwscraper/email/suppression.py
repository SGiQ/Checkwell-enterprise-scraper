"""Suppression list — addresses we must never email.

File-backed JSON store at ``<data_dir>/suppression.json``. Entries land here
when:

  - a recipient hits the public /unsubscribe link
  - the inbound poller classifies a reply as ``unsubscribe`` or ``bounce``
  - an operator adds an address manually from the dashboard

The dispatcher checks ``SuppressionList.is_suppressed(to_email)`` before each
send and skips suppressed recipients with a clear status. Matches the
``ScheduledEmailQueue`` storage pattern so a future Postgres migration can
swap both classes in one pass.

Entry shape::

    {
      "email":      "owner@example.com",
      "reason":     "unsubscribe",     # unsubscribe|bounce|manual|complaint
      "notes":      "via /unsubscribe one-click",
      "added_by":   "system" | <username>,
      "created_at": "2026-05-21T03:16:05+00:00"
    }
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

VALID_REASONS = ("unsubscribe", "bounce", "manual", "complaint")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(addr: str) -> str:
    return (addr or "").strip().lower()


class SuppressionList:
    """File-backed do-not-contact list. Reads/writes data/suppression.json."""

    def __init__(self, data_dir: Path):
        self.file = data_dir / "suppression.json"
        self._lock = threading.RLock()

    # --- read paths ---

    def _all(self) -> list[dict]:
        if not self.file.exists():
            return []
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def list_all(self, limit: int | None = None) -> list[dict]:
        rows = self._all()
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows[:limit] if limit else rows

    def is_suppressed(self, email: str) -> bool:
        target = _normalize(email)
        if not target:
            return True   # empty / None addresses are never sendable
        return any(_normalize(r.get("email", "")) == target for r in self._all())

    def get(self, email: str) -> dict | None:
        target = _normalize(email)
        for r in self._all():
            if _normalize(r.get("email", "")) == target:
                return r
        return None

    # --- write paths ---

    def add(
        self,
        email: str,
        *,
        reason: str = "manual",
        notes: str = "",
        added_by: str = "system",
    ) -> dict | None:
        """Add an address. Idempotent — re-adding updates reason/notes."""
        target = _normalize(email)
        if not target or "@" not in target:
            return None
        if reason not in VALID_REASONS:
            reason = "manual"

        with self._lock:
            rows = self._all()
            for r in rows:
                if _normalize(r.get("email", "")) == target:
                    # Refresh metadata but preserve original created_at.
                    r.update({
                        "reason":   reason,
                        "notes":    notes or r.get("notes", ""),
                        "added_by": added_by or r.get("added_by", "system"),
                    })
                    self._write(rows)
                    return r
            entry = {
                "email":      target,
                "reason":     reason,
                "notes":      notes,
                "added_by":   added_by,
                "created_at": _utcnow_iso(),
            }
            rows.append(entry)
            self._write(rows)
            return entry

    def remove(self, email: str) -> bool:
        """Remove an address from suppression. Returns True if found+removed."""
        target = _normalize(email)
        if not target:
            return False
        with self._lock:
            rows = self._all()
            new_rows = [r for r in rows if _normalize(r.get("email", "")) != target]
            if len(new_rows) == len(rows):
                return False
            self._write(new_rows)
            return True

    # --- internals ---

    def _write(self, rows: list[dict]) -> None:
        self.file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
