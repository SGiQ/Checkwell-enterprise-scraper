"""Scheduled-email queue, persisted as JSON next to the other repo files.

A scheduled email entry:
    {
      "id": "uuid",
      "prospect_id": "biz-abc",
      "lead_type": "business",     # 'business' or 'community'
      "to_email": "owner@example.com",
      "subject": "...",
      "body": "...",
      "body_html": "...",          # optional HTML alternative
      "from_email": "shaun@checkwellcall.com",
      "from_name":  "Shaun (CheckWellCall)",
      "reply_to":   "shaun@checkwellcall.com",  # optional
      "scheduled_for": "2026-05-20T14:00:00Z",  # ISO UTC
      "status": "pending",         # pending | sending | sent | failed | cancelled
      "created_at": "2026-05-16T...",
      "claimed_at": "",            # set when a dispatcher claims it for sending
      "attempts": 0,               # send attempts so far (for retry/backoff)
      "sent_at": "",               # set when sent
      "provider_id": "",           # transport's message id
      "error": "",                 # set when failed
    }

Durability notes:
  * Writes are atomic (temp file + os.replace) with a `.bak` of the last good
    state, so a crash mid-write can't corrupt or lose the queue.
  * Sending is at-most-once: `claim_due` flips an entry to 'sending' BEFORE the
    send, so a crash mid-send leaves it as 'sending' (not 'pending'), and
    `recover_stale` fails it rather than blindly resending — for cold email,
    missing one send beats double-sending.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScheduledEmailQueue:
    """File-backed queue. Reads/writes data/scheduled_emails.json."""

    def __init__(self, data_dir: Path):
        self.file = data_dir / "scheduled_emails.json"
        self._bak = self.file.with_suffix(".json.bak")
        self._tmp = self.file.with_suffix(".json.tmp")
        self._lock = threading.RLock()

    # --- read paths ---

    @staticmethod
    def _read_file(path: Path) -> list[dict] | None:
        """Parse a queue file. Returns None (not []) on missing/corrupt, so the
        caller can tell 'no file / unreadable' apart from a valid empty queue."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else None
        except (json.JSONDecodeError, OSError):
            return None

    def _all(self) -> list[dict]:
        rows = self._read_file(self.file)
        if rows is not None:
            return rows
        # Main file missing/corrupt — fall back to the last good backup.
        rows = self._read_file(self._bak)
        return rows if rows is not None else []

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
        """Pending emails whose scheduled_for has passed (read-only — does not
        claim). Kept for callers/tests that just want to inspect the due set;
        the dispatcher uses claim_due()."""
        cutoff = now_iso or _utcnow_iso()
        return [
            r for r in self._all()
            if r.get("status") == "pending"
            and r.get("scheduled_for", "") <= cutoff
        ]

    # --- write paths ---

    @staticmethod
    def _new_entry(
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
        return {
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
            "claimed_at": "",
            "attempts": 0,
            "sent_at": "",
            "provider_id": "",
            "error": "",
        }

    def enqueue(self, **fields) -> dict:
        entry = self._new_entry(**fields)
        with self._lock:
            rows = self._all()
            rows.append(entry)
            self._write(rows)
        return entry

    # Committed volume for cap counting: a 'sending'/'sent' email is volume
    # already spent; a 'pending' one is volume committed for today.
    _COMMITTED = ("pending", "sending", "sent")

    def enqueue_checked(
        self,
        *,
        daily_cap: int,
        per_domain_cap: int,
        today: str,
        **fields,
    ) -> tuple[dict | None, str]:
        """Enforce the daily + per-domain caps and enqueue **atomically**.

        Counting today's committed sends and writing the new entry happen under
        ONE lock, so two concurrent callers can't both pass the count check and
        then both enqueue (the TOCTOU race that let sends overshoot the cap).
        Returns (entry, "") on success, or (None, reason) when a cap is hit.
        `today` is the YYYY-MM-DD (UTC) prefix matched against scheduled_for.
        """
        to_email = fields.get("to_email", "") or ""
        domain = to_email.split("@", 1)[1].strip().lower() if "@" in to_email else ""
        with self._lock:
            rows = self._all()
            total = 0
            dom = 0
            for r in rows:
                if r.get("status") not in self._COMMITTED:
                    continue
                if (r.get("scheduled_for") or "")[:10] != today:
                    continue
                total += 1
                if domain:
                    rt = r.get("to_email") or ""
                    rd = rt.split("@", 1)[1].strip().lower() if "@" in rt else ""
                    if rd == domain:
                        dom += 1
            if total >= daily_cap:
                return None, f"daily-cap ({total}/{daily_cap} used today)"
            if per_domain_cap and domain and dom >= per_domain_cap:
                return None, f"per-domain-cap (@{domain}: {dom}/{per_domain_cap} today)"
            entry = self._new_entry(**fields)
            rows.append(entry)
            self._write(rows)
            return entry, ""

    def claim_due(self, now_iso: str | None = None) -> list[dict]:
        """Atomically claim due-pending emails for sending.

        Flips each to 'sending' + stamps claimed_at BEFORE returning, so a crash
        mid-send doesn't leave the entry as 'pending' to be resent. Returns copies
        of the claimed entries.
        """
        cutoff = now_iso or _utcnow_iso()
        with self._lock:
            rows = self._all()
            claimed: list[dict] = []
            for r in rows:
                if r.get("status") == "pending" and r.get("scheduled_for", "") <= cutoff:
                    r["status"] = "sending"
                    r["claimed_at"] = cutoff
                    claimed.append(dict(r))
            if claimed:
                self._write(rows)
            return claimed

    def recover_stale(self, lease_seconds: int = 300, now_iso: str | None = None) -> int:
        """Fail any entry stuck in 'sending' past the lease — the dispatcher died
        mid-send. NOT resent (at-most-once). Returns the number recovered. Run on
        startup. An operator can manually requeue if they confirm it never sent."""
        now = _parse_iso(now_iso or _utcnow_iso())
        with self._lock:
            rows = self._all()
            n = 0
            for r in rows:
                if r.get("status") != "sending":
                    continue
                claimed = _parse_iso(r.get("claimed_at", ""))
                age = (now - claimed).total_seconds() if claimed else lease_seconds + 1
                if age >= lease_seconds:
                    r["status"] = "failed"
                    r["error"] = (
                        "interrupted mid-send (claim lease expired); not auto-resent "
                        "to avoid a double-send"
                    )
                    n += 1
            if n:
                self._write(rows)
            return n

    def mark_sent(self, email_id: str, provider_id: str = "") -> dict | None:
        return self._patch(email_id, {
            "status": "sent",
            "sent_at": _utcnow_iso(),
            "claimed_at": "",
            "provider_id": provider_id,
            "error": "",
        })

    def mark_failed(self, email_id: str, error: str) -> dict | None:
        return self._patch(email_id, {
            "status": "failed",
            "claimed_at": "",
            "error": (error or "")[:300],
        })

    def reschedule(self, email_id: str, scheduled_for: str, error: str = "") -> dict | None:
        """Return a retryable send to 'pending' at a later time, bumping attempts.
        Used by the dispatcher's transient-failure backoff."""
        with self._lock:
            rows = self._all()
            updated = None
            for r in rows:
                if r.get("id") == email_id:
                    r["status"] = "pending"
                    r["scheduled_for"] = scheduled_for
                    r["attempts"] = int(r.get("attempts", 0)) + 1
                    r["claimed_at"] = ""
                    r["error"] = (error or "")[:300]
                    updated = r
                    break
            if updated is not None:
                self._write(rows)
            return updated

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
        """Atomic write: serialize to a temp file, back up the last good state,
        then os.replace into place (atomic on the same filesystem). A crash can
        never leave a half-written main file."""
        data = json.dumps(rows, indent=2)
        self._tmp.write_text(data, encoding="utf-8")
        try:
            if self.file.exists():
                # Copy (not move) the current good file to .bak before swapping.
                self._bak.write_text(self.file.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass  # backup is best-effort; the atomic replace below is what matters
        os.replace(self._tmp, self.file)


def _parse_iso(s: str):
    """Parse an ISO-8601 string to an aware datetime, or None if unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
