"""Queue of auto-drafted replies awaiting operator review.

When the inbound poller classifies a recipient's reply as ``interested``,
:mod:`cwscraper.replies.auto_reply` drafts a response. That draft lands
here. The operator reviews from the dashboard, then either:

  - **Approves** — moves it into the scheduled-emails queue, scheduled
    a few minutes out, dispatched by the existing send loop.
  - **Dismisses** — discards the draft (no email goes out).

This is a separate JSON file from ``scheduled_emails.json`` because the
semantics are different: scheduled emails are committed to send;
inbound-drafts are *proposed* sends pending human approval.

Entry shape::

    {
      "id": "uuid",
      "business_id": "biz_abc",
      "business_name": "Sunshine Home Care",
      "to_email": "owner@example.com",
      "subject": "Re: Your outreach about wellness checks",
      "body": "...the AI-drafted response...",
      "original_reply_excerpt": "what they said (for operator context)",
      "booking_link": "https://cal.com/...",
      "demo_video_link": "https://loom.com/...",
      "ai_error": null,                  # populated if LLM failed
      "status": "pending",                # pending|approved|dismissed
      "created_at": "2026-05-21T...",
      "decided_at": "",
      "decided_by": "",                   # "operator" once acted on
      "approved_queue_id": ""             # set when status=approved
    }
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InboundDraftQueue:
    """File-backed queue. Reads/writes data/inbound_drafts.json."""

    def __init__(self, data_dir: Path):
        self.file = data_dir / "inbound_drafts.json"
        self._lock = threading.RLock()

    # --- read paths ---

    def _all(self) -> list[dict]:
        if not self.file.exists():
            return []
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def list(self, status: str | None = None) -> list[dict]:
        rows = self._all()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        # Newest first; pending before terminal
        rows.sort(key=lambda r: (r.get("status") != "pending", r.get("created_at", "")), reverse=False)
        # Pending first, then by created_at desc within each bucket
        pending = sorted(
            [r for r in rows if r.get("status") == "pending"],
            key=lambda r: r.get("created_at", ""), reverse=True,
        )
        terminal = sorted(
            [r for r in rows if r.get("status") != "pending"],
            key=lambda r: r.get("created_at", ""), reverse=True,
        )
        return pending + terminal

    def get(self, draft_id: str) -> dict | None:
        for r in self._all():
            if r.get("id") == draft_id:
                return r
        return None

    def pending_for_business(self, business_id: str) -> dict | None:
        """Return the existing pending draft for this business, if any.

        Prevents auto-creating duplicate drafts when the recipient sends
        multiple replies (e.g. a follow-up clarification before the operator
        has reviewed the first).
        """
        for r in self._all():
            if r.get("status") == "pending" and r.get("business_id") == business_id:
                return r
        return None

    # --- write paths ---

    def enqueue(self, draft: dict) -> dict:
        """Add a new draft. Returns the persisted entry (with ID + timestamps)."""
        entry = {
            "id": uuid.uuid4().hex,
            "business_id": draft.get("business_id", ""),
            "business_name": draft.get("business_name", ""),
            "to_email": draft.get("to_email", ""),
            "subject": draft.get("subject", ""),
            "body": draft.get("body", ""),
            "original_reply_excerpt": draft.get("original_reply_excerpt", ""),
            "booking_link": draft.get("booking_link", ""),
            "demo_video_link": draft.get("demo_video_link", ""),
            "ai_error": draft.get("error") or draft.get("ai_error"),
            "status": "pending",
            "created_at": _utcnow_iso(),
            "decided_at": "",
            "decided_by": "",
            "approved_queue_id": "",
        }
        with self._lock:
            rows = self._all()
            rows.append(entry)
            self._write(rows)
        return entry

    def update_body(self, draft_id: str, *, subject: str | None = None,
                    body: str | None = None) -> dict | None:
        """Operator-edited subject/body (for review-edit-approve flow)."""
        patch: dict = {}
        if subject is not None:
            patch["subject"] = subject
        if body is not None:
            patch["body"] = body
        if not patch:
            return self.get(draft_id)
        return self._patch(draft_id, patch)

    def mark_approved(self, draft_id: str, *, queue_id: str,
                      decided_by: str = "operator") -> dict | None:
        return self._patch(draft_id, {
            "status": "approved",
            "decided_at": _utcnow_iso(),
            "decided_by": decided_by,
            "approved_queue_id": queue_id,
        })

    def mark_dismissed(self, draft_id: str, *, decided_by: str = "operator") -> dict | None:
        return self._patch(draft_id, {
            "status": "dismissed",
            "decided_at": _utcnow_iso(),
            "decided_by": decided_by,
        })

    # --- internals ---

    def _patch(self, draft_id: str, patch: dict) -> dict | None:
        with self._lock:
            rows = self._all()
            updated = None
            for r in rows:
                if r.get("id") == draft_id:
                    r.update(patch)
                    updated = r
                    break
            if updated is not None:
                self._write(rows)
            return updated

    def _write(self, rows: list[dict]) -> None:
        self.file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
