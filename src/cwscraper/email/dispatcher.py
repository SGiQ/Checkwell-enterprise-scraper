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
from cwscraper.email.send_limits import record_first_send_date_if_unset
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
        # Serializes tick(): the periodic loop and the /send-now immediate tick
        # both call tick() and would otherwise both claim the same due entry
        # (the queue's per-write lock doesn't span read→send→mark). One tick at a
        # time = no in-process double-send.
        self._tick_lock = threading.Lock()

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
        """Process every email due right now. Returns count sent.

        Non-reentrant: if another tick is already draining the queue (e.g. the
        periodic loop while /send-now also fires), this returns 0 immediately
        rather than letting two threads claim the same entry. The in-flight tick
        sends everything currently due; anything scheduled just after is picked
        up on the next tick.
        """
        if not self._tick_lock.acquire(blocking=False):
            return 0
        try:
            return self._tick_locked()
        finally:
            self._tick_lock.release()

    def _tick_locked(self) -> int:
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
                self._notify_crm(entry, "failed", error="no email transport configured")
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
                self._notify_crm(entry, "suppressed", error="recipient on suppression list")
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
                    body_html=entry.get("body_html", ""),
                    from_email=entry.get("from_email", ""),
                    from_name=entry.get("from_name", ""),
                    reply_to=entry.get("reply_to", ""),
                )
                provider_id = result.get("provider_id", "")
                self.queue.mark_sent(entry["id"], provider_id=provider_id)
                self._record_send_on_prospect(entry)
                # Stamp the first-send date so the warm-up curve has a
                # reference point. Idempotent — only writes once.
                record_first_send_date_if_unset()
                self._notify_crm(entry, "sent", provider_id=provider_id)
                sent_count += 1
                logger.info("Sent scheduled email %s to %s", entry["id"], entry["to_email"])
            except TransportError as e:
                self.queue.mark_failed(entry["id"], str(e))
                self._notify_crm(entry, "failed", error=str(e))
                logger.error("Failed to send %s: %s", entry["id"], e)
            except Exception as e:
                self.queue.mark_failed(entry["id"], f"{type(e).__name__}: {e}")
                self._notify_crm(entry, "failed", error=f"{type(e).__name__}: {e}")
                logger.exception("Unhandled error sending %s", entry["id"])
        return sent_count

    def _notify_crm(self, entry: dict, status: str, *, provider_id: str = "", error: str = "") -> None:
        """Best-effort delivery-status callback to the CRM (no-op for non-CRM
        emails or when CRM env isn't configured). Never raises into the tick loop."""
        try:
            from cwscraper.integrations.crm import notify_email_status
            notify_email_status(entry, status, provider_id=provider_id, error=error)
        except Exception:  # noqa: BLE001 — never let the callback break dispatch
            logger.debug("CRM email-status notify failed", exc_info=True)

    def _record_send_on_prospect(self, entry: dict) -> None:
        """Stamp the prospect: activity log entry + auto-advance pipeline."""
        prospect_id = entry.get("prospect_id", "")
        lead_type = entry.get("lead_type", "business")
        # CRM-originated sends (routed in via /api/emails/send-external) have no
        # scraper prospect to advance — skip the pipeline update entirely.
        if lead_type == "external" or prospect_id.startswith("crm:"):
            return
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
