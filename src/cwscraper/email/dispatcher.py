"""Background thread that wakes every TICK_SECONDS, pulls due-pending emails
from the queue, sends them via the configured transport, and updates status.

Also writes an entry to the prospect's activity_log + advances pipeline_stage
to 'outreach_sent' on first successful send.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from cwscraper.email.queue import ScheduledEmailQueue
from cwscraper.email.suppression import SuppressionList
from cwscraper.email.transport import EmailTransport, TransportError, get_transport

logger = logging.getLogger("cwscraper.email.dispatcher")

TICK_SECONDS = 60   # how often to check the queue


class EmailDispatcher:
    """Polls the queue and sends due emails."""

    def __init__(
        self,
        queue: ScheduledEmailQueue,
        repo,
        suppression: SuppressionList | None = None,
    ):
        self.queue = queue
        self.repo = repo
        # Optional: when present, every send checks here first and skips
        # suppressed recipients. Kept optional so existing callers (older
        # tests, partial wiring) don't have to pass it.
        self.suppression = suppression
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="email-dispatcher", daemon=True
        )
        self._thread.start()
        logger.info("Email dispatcher started")

    def stop(self) -> None:
        self._stop.set()
        logger.info("Email dispatcher stop signal sent")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.exception("dispatcher tick failed: %s", e)
            self._stop.wait(TICK_SECONDS)

    def tick(self) -> int:
        """Process every email due right now. Returns count sent."""
        due = self.queue.due_pending()
        if not due:
            return 0

        transport = get_transport()
        if transport is None:
            # No transport configured — mark them failed with a clear msg
            for entry in due:
                self.queue.mark_failed(
                    entry["id"],
                    "No email transport configured. Set RESEND_API_KEY + "
                    "CWSCRAPER_FROM_EMAIL in Railway."
                )
            return 0

        sent_count = 0
        for entry in due:
            # Suppression check — never send to addresses that have unsubscribed,
            # bounced, or been added to the do-not-contact list. We mark the
            # queue entry failed with a clear reason so the operator can see
            # what happened in the Pipeline > Scheduled view.
            if self.suppression and self.suppression.is_suppressed(entry["to_email"]):
                self.queue.mark_failed(
                    entry["id"],
                    f"Suppressed: {entry['to_email']} is on the do-not-contact list",
                )
                logger.info(
                    "Skipped send to %s (suppressed) — queue entry %s",
                    entry["to_email"], entry["id"],
                )
                continue
            try:
                result = transport.send(
                    to_email=entry["to_email"],
                    subject=entry["subject"],
                    body_text=entry["body"],
                    from_email=entry.get("from_email", ""),
                    from_name=entry.get("from_name", ""),
                    reply_to=entry.get("reply_to", ""),
                )
                self.queue.mark_sent(entry["id"], provider_id=result.get("provider_id", ""))
                self._record_send_on_prospect(entry)
                sent_count += 1
                logger.info("Sent scheduled email %s to %s", entry["id"], entry["to_email"])
            except TransportError as e:
                self.queue.mark_failed(entry["id"], str(e))
                logger.error("Failed to send %s: %s", entry["id"], e)
            except Exception as e:
                self.queue.mark_failed(entry["id"], f"{type(e).__name__}: {e}")
                logger.exception("Unhandled error sending %s", entry["id"])
        return sent_count

    def _record_send_on_prospect(self, entry: dict) -> None:
        """Stamp the prospect: activity log entry + auto-advance pipeline."""
        prospect_id = entry.get("prospect_id", "")
        lead_type = entry.get("lead_type", "business")
        if not prospect_id:
            return

        # Update the prospect (auto-appends to activity_log via repo.update_prospect)
        # We patch pipeline_stage only if currently 'new' or 'qualified' so we don't
        # downgrade a prospect that's further along.
        prospects = self.repo.get_all_prospects()
        match = next((p for p in prospects if p.get("id") == prospect_id), None)
        if not match:
            return

        patch: dict = {}
        current_stage = match.get("pipeline_stage", "new")
        if current_stage in ("new", "qualified"):
            patch["pipeline_stage"] = "outreach_sent"

        # We always log the send, even if stage didn't move
        # Use a dedicated action so it's distinguishable in the activity panel
        self.repo.update_prospect(
            prospect_id, lead_type,
            patch or {"pipeline_stage": current_stage},  # no-op patch keeps activity log working
            action="email_sent",
        )
