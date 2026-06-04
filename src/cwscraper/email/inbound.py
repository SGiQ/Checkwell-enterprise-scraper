"""IMAP-based inbound email polling — closes the outreach loop.

Background thread that wakes every ``IMAP_POLL_MINUTES``, fetches UNSEEN
messages from the configured mailbox, classifies each reply, and updates
the matching prospect:

  - ``interested``      -> stage ``reply_received`` (always wins)
  - ``not_interested``  -> stage ``lost``
  - ``unsubscribe``     -> stage ``lost`` + add to suppression list
  - ``bounce``          -> add to suppression list (stage left as-is)
  - ``out_of_office``   -> activity log only — no stage change
  - ``unclear``         -> activity log only — operator triages from dashboard

Matching strategy: From-address of the reply is looked up against
``business.email`` first, then any ``contact.email`` on the business.
Community-mode leads aren't tied to email addresses, so they're skipped.

Pluggable design: uses stdlib :mod:`imaplib` so no new dependency. Auth
falls back to the SMTP creds if the IMAP_* vars aren't set separately
(useful for single-mailbox providers like Hostinger / Workspace).
"""
from __future__ import annotations

import email as email_module
import imaplib
import logging
import os
import re
import threading
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Iterable

from cwscraper.email.suppression import SuppressionList

logger = logging.getLogger("cwscraper.email.inbound")


# ---------------------------------------------------------------------------
# Classifier — keyword-based; deliberately conservative so unclear stays unclear
# ---------------------------------------------------------------------------

REPLY_CLASSIFIERS = (
    ("unsubscribe", re.compile(
        r"\b(unsubscribe|remove me|opt[\s-]?out|take me off|stop emailing|"
        r"do not (email|contact)|please stop)\b", re.I)),
    ("out_of_office", re.compile(
        r"\b(out of (the )?office|on vacation|annual leave|"
        r"away from my desk|auto[-\s]?reply|automatic reply)\b", re.I)),
    ("not_interested", re.compile(
        r"\b(not interested|no thanks|no thank you|please don.?t|"
        r"wrong person|not the right|we'?re all set|we are all set)\b", re.I)),
    ("interested", re.compile(
        r"\b(interested|tell me more|sounds good|let.?s (talk|chat|connect)|"
        r"schedule|book a|call me|when can we|happy to|love to learn)\b", re.I)),
)

BOUNCE_FROM_PATTERNS = (
    "mailer-daemon", "postmaster", "mail-delivery", "delivery-subsystem",
    "noreply-dmarc", "bounces+",
)

BOUNCE_SUBJECT_RE = re.compile(
    r"(delivery (status notification|failed)|undeliverable|"
    r"address (rejected|not found)|returned mail)", re.I,
)


def classify_reply(from_address: str, subject: str, body: str) -> str:
    """Return one of: ``bounce``, ``unsubscribe``, ``out_of_office``,
    ``not_interested``, ``interested``, ``unclear``.

    Ordering matters: bounce wins (envelope), then unsubscribe (legal
    obligation to honor), then OOO (don't downgrade a real reply we'll
    get later), then negative, then positive.
    """
    addr = (from_address or "").lower()
    if any(s in addr for s in BOUNCE_FROM_PATTERNS):
        return "bounce"
    haystack = f"{subject or ''}\n{body or ''}"
    if BOUNCE_SUBJECT_RE.search(haystack):
        return "bounce"
    for label, pat in REPLY_CLASSIFIERS:
        if pat.search(haystack):
            return label
    return "unclear"


# ---------------------------------------------------------------------------
# Body extraction — pluck text/plain (fall back to text/html stripped)
# ---------------------------------------------------------------------------

def _extract_body(msg) -> tuple[str, str]:
    """Return (text_body, html_body) — either may be empty."""
    text_body, html_body = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and not text_body:
                text_body = decoded
            elif ctype == "text/html" and not html_body:
                html_body = decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace") if payload else ""
            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                text_body = decoded
        except Exception:
            pass
    return text_body, html_body


# ---------------------------------------------------------------------------
# IMAP transport — thin wrapper so tick() stays testable with mocks
# ---------------------------------------------------------------------------

class ImapClient:
    """Connect / fetch UNSEEN / mark Seen. Thin shim over imaplib."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    def fetch_unseen(self, mailbox: str = "INBOX", max_messages: int = 100) -> list[bytes]:
        """Return raw RFC-822 bytes of UNSEEN messages, marking them \\Seen."""
        with imaplib.IMAP4_SSL(self.host, self.port) as M:
            M.login(self.user, self.password)
            M.select(mailbox)
            typ, data = M.search(None, "(UNSEEN)")
            if typ != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()[:max_messages]
            out: list[bytes] = []
            for num in ids:
                typ, msg_data = M.fetch(num, "(RFC822)")
                if typ == "OK" and msg_data and msg_data[0]:
                    out.append(msg_data[0][1])
            return out

    def can_connect(self) -> tuple[bool, str]:
        """Smoke-check IMAP creds. Used by the preflight banner."""
        try:
            with imaplib.IMAP4_SSL(self.host, self.port) as M:
                M.login(self.user, self.password)
                M.select("INBOX")
            return True, ""
        except Exception as e:
            return False, str(e)


# ---------------------------------------------------------------------------
# Env-driven config helpers
# ---------------------------------------------------------------------------

def _env_imap_settings() -> dict:
    """Resolve IMAP creds with smart fallbacks to SMTP creds."""
    return {
        "host": os.getenv("IMAP_HOST", "").strip(),
        "port": int(os.getenv("IMAP_PORT", "993").strip() or "993"),
        "user": os.getenv("IMAP_USER", "").strip() or os.getenv("SMTP_USER", "").strip(),
        "password": (
            os.getenv("IMAP_PASSWORD", "").strip()
            or os.getenv("SMTP_PASSWORD", "").strip()
        ),
        "poll_minutes": max(1, int(os.getenv("IMAP_POLL_MINUTES", "10").strip() or "10")),
        "enabled": os.getenv("IMAP_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
    }


def _imap_configured(settings: dict | None = None) -> bool:
    s = settings or _env_imap_settings()
    return bool(s["host"] and s["user"] and s["password"])


# ---------------------------------------------------------------------------
# Poller — long-running background thread
# ---------------------------------------------------------------------------

class InboundEmailPoller:
    """Polls the IMAP inbox for replies, classifies, updates prospects."""

    def __init__(self, repo, suppression: SuppressionList,
                 inbound_drafts=None):
        self.repo = repo
        self.suppression = suppression
        # Optional queue for AI-drafted second-touch responses to
        # 'interested' replies. When provided, the poller auto-drafts a
        # reply (via cwscraper.replies.auto_reply) and queues it here for
        # operator review. When absent, the inbound flow behaves exactly
        # as before — classifier + stage transition only.
        self.inbound_drafts = inbound_drafts
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Cache settings at construct-time; re-read on each tick so env
        # changes (Railway redeploy) take effect without a process restart.

    # --- lifecycle ---

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="email-inbound-poller", daemon=True
        )
        self._thread.start()
        logger.info("Inbound email poller started")

    def stop(self) -> None:
        self._stop.set()
        logger.info("Inbound email poller stop signal sent")

    def _loop(self) -> None:
        # First tick happens immediately so a freshly-started poller is
        # responsive; subsequent ticks honor IMAP_POLL_MINUTES.
        while not self._stop.is_set():
            settings = _env_imap_settings()
            if settings["enabled"] and _imap_configured(settings):
                try:
                    self.tick()
                except Exception as e:
                    logger.exception("inbound poller tick failed: %s", e)
            else:
                logger.debug("inbound poller dormant (enabled=%s configured=%s)",
                             settings["enabled"], _imap_configured(settings))
            interval = settings["poll_minutes"] * 60
            self._stop.wait(interval)

    # --- one fetch+classify pass ---

    def tick(self, client: ImapClient | None = None) -> dict:
        """Fetch UNSEEN, classify, update prospects. Returns summary dict.

        ``client`` is injectable for testing.
        """
        settings = _env_imap_settings()
        if not _imap_configured(settings):
            return {"fetched": 0, "skipped_unconfigured": True}

        if client is None:
            client = ImapClient(
                host=settings["host"], port=settings["port"],
                user=settings["user"], password=settings["password"],
            )

        try:
            raw_msgs = client.fetch_unseen()
        except Exception as e:
            logger.exception("IMAP fetch failed")
            return {"fetched": 0, "error": str(e)}

        summary: dict = {"fetched": len(raw_msgs), "classified": {}, "matched": 0, "unmatched": 0}

        for raw in raw_msgs:
            try:
                msg = email_module.message_from_bytes(raw)
            except Exception:
                continue

            from_raw = msg.get("From", "") or ""
            _, from_addr = parseaddr(from_raw)
            subject = msg.get("Subject", "") or ""
            text_body, html_body = _extract_body(msg)
            label = classify_reply(from_addr, subject, text_body or html_body)
            summary["classified"][label] = summary["classified"].get(label, 0) + 1

            # Best-effort: forward every classified reply to the SGiQ CRM, which
            # matches by email within its tenant and halts any active campaign
            # drip. Non-matches are ignored CRM-side. Independent of the scraper's
            # own prospect matching below.
            self._notify_crm_reply(from_addr, label, subject, text_body or html_body)

            prospect = self._find_prospect_by_email(from_addr)
            if prospect:
                summary["matched"] += 1
                self._apply_reply_to_prospect(
                    prospect, label, from_addr, subject,
                    reply_body=text_body or html_body,
                )
            else:
                summary["unmatched"] += 1
                # Still honor unsubscribes & record bounces even with no prospect link.
                if label == "unsubscribe":
                    self.suppression.add(
                        from_addr, reason="unsubscribe",
                        notes=f"reply (unmatched): {subject[:120]}",
                    )
                elif label == "bounce":
                    self.suppression.add(
                        from_addr, reason="bounce",
                        notes=subject[:200],
                    )

        return summary

    def _notify_crm_reply(self, from_addr: str, label: str, subject: str, body: str) -> None:
        """Best-effort forward of a classified reply to the CRM. Never raises."""
        try:
            from cwscraper.integrations.crm import notify_email_reply
            notify_email_reply(
                from_email=from_addr,
                classification=label,
                subject=subject or "",
                snippet=body or "",
            )
        except Exception:  # noqa: BLE001 — never let the forward break the poll loop
            logger.debug("CRM reply notify failed", exc_info=True)

    # --- prospect lookup + state machine ---

    def _find_prospect_by_email(self, addr: str) -> dict | None:
        """Match a reply's From: to a prospect via business.email or contact.email."""
        target = (addr or "").strip().lower()
        if not target or "@" not in target:
            return None
        for p in self.repo.get_all_prospects():
            if p.get("lead_type") != "business":
                continue
            biz_email = (p.get("email") or "").strip().lower()
            if biz_email and biz_email == target:
                return p
            for c in p.get("contacts") or []:
                if (c.get("email") or "").strip().lower() == target:
                    return p
        return None

    def _apply_reply_to_prospect(
        self, prospect: dict, label: str, from_addr: str, subject: str,
        *, reply_body: str = "",
    ) -> None:
        """Translate classifier verdict into pipeline movement + suppression.

        When ``label == 'interested'`` AND an :class:`InboundDraftQueue` was
        provided at construction, also auto-drafts a second-touch response
        and queues it for operator review (no auto-send).
        """
        prospect_id = prospect.get("id", "")
        lead_type = prospect.get("lead_type", "business")
        current_stage = prospect.get("pipeline_stage", "new")

        # Always record the inbound on the activity log; pipeline_stage patch
        # is layered on top depending on the verdict.
        patch: dict = {}

        if label == "interested":
            # Don't downgrade meetings/customers back to reply_received.
            if current_stage in ("new", "qualified", "outreach_sent"):
                patch["pipeline_stage"] = "reply_received"
            # Auto-draft a second-touch reply for the operator to review.
            # Only one pending draft per business — multiple replies from the
            # same recipient before review consolidate into the first one.
            if self.inbound_drafts and not self.inbound_drafts.pending_for_business(prospect_id):
                try:
                    from cwscraper.replies.auto_reply import draft_auto_reply
                    draft = draft_auto_reply(
                        business=prospect,
                        reply_text=reply_body,
                        reply_subject=subject,
                    )
                    self.inbound_drafts.enqueue(draft.to_dict())
                    logger.info(
                        "auto-drafted reply for business %s (ai_error=%s)",
                        prospect_id, draft.error,
                    )
                except Exception as e:
                    logger.exception(
                        "auto-draft failed for business %s: %s", prospect_id, e,
                    )
        elif label in ("not_interested", "unsubscribe"):
            patch["pipeline_stage"] = "lost"
            if label == "unsubscribe":
                self.suppression.add(
                    from_addr, reason="unsubscribe",
                    notes=f"reply: {subject[:120]}",
                )
        elif label == "bounce":
            self.suppression.add(from_addr, reason="bounce", notes=subject[:200])
        # out_of_office + unclear -> activity log only

        # Always call update_prospect even when patch is empty, so the activity
        # log captures the inbound. Use a no-op patch (current stage) when needed.
        action = f"reply_{label}"
        self.repo.update_prospect(
            prospect_id, lead_type,
            patch or {"pipeline_stage": current_stage},
            action=action,
        )


# ---------------------------------------------------------------------------
# Module-level helpers used by routes + preflight
# ---------------------------------------------------------------------------

def inbound_settings_summary() -> dict:
    """For the preflight banner / /api/emails/inbound endpoint."""
    s = _env_imap_settings()
    return {
        "enabled": s["enabled"],
        "configured": _imap_configured(s),
        "host": s["host"],
        "port": s["port"],
        "user_masked": _mask_email(s["user"]),
        "poll_minutes": s["poll_minutes"],
        "fallback_to_smtp_creds": bool(
            not os.getenv("IMAP_USER") and not os.getenv("IMAP_PASSWORD")
            and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD")
        ),
    }


def _mask_email(addr: str) -> str:
    if not addr or "@" not in addr:
        return ""
    local, domain = addr.split("@", 1)
    if len(local) <= 2:
        return f"{local[0:1]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"
