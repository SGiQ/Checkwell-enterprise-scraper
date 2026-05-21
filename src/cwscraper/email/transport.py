"""Pluggable email transports.

v1 ships two: Resend (API) and SMTP (works for Gmail, Workspace, Outlook,
Mailgun, etc.). Both implement the same EmailTransport protocol — the rest
of the system only sees the abstract interface.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Protocol
from urllib.parse import quote, urlencode, urlparse

import requests

logger = logging.getLogger("cwscraper.email")


def _unsubscribe_headers(to_email: str, from_email: str) -> dict[str, str]:
    """Build List-Unsubscribe + List-Unsubscribe-Post headers when configured.

    Returns an empty dict when CWSCRAPER_UNSUBSCRIBE_URL isn't set, so the
    transports stay non-prescriptive (the operator can opt out of cold-outreach
    behavior for transactional sends by simply leaving the var unset).
    """
    base = os.getenv("CWSCRAPER_UNSUBSCRIBE_URL", "").strip()
    if not base:
        return {}
    # Append ?email=... so the public page knows who clicked.
    sep = "&" if urlparse(base).query else "?"
    link = f"{base}{sep}{urlencode({'email': to_email})}"
    mailto = (
        f"mailto:{from_email}?subject=unsubscribe"
        if from_email else ""
    )
    list_unsub = f"<{link}>"
    if mailto:
        list_unsub += f", <{mailto}>"
    return {
        "List-Unsubscribe": list_unsub,
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


class TransportError(Exception):
    """Raised when a transport fails to send an email."""


class EmailTransport(Protocol):
    """All email transports honor this contract."""

    name: str

    def send(
        self,
        *,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str = "",
        from_email: str = "",
        from_name: str = "",
        reply_to: str = "",
    ) -> dict: ...

    @property
    def configured(self) -> bool: ...


class ResendTransport:
    """Resend (https://resend.com). API: POST /emails with bearer token."""

    name = "resend"
    endpoint = "https://api.resend.com/emails"

    def __init__(self):
        self.api_key = os.getenv("RESEND_API_KEY", "").strip()
        self.default_from_email = os.getenv("CWSCRAPER_FROM_EMAIL", "").strip()
        self.default_from_name = os.getenv("CWSCRAPER_FROM_NAME", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.default_from_email)

    def send(
        self,
        *,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str = "",
        from_email: str = "",
        from_name: str = "",
        reply_to: str = "",
    ) -> dict:
        if not self.configured:
            raise TransportError(
                "Resend transport not configured. Set RESEND_API_KEY and "
                "CWSCRAPER_FROM_EMAIL (and ideally CWSCRAPER_FROM_NAME)."
            )

        from_email = from_email or self.default_from_email
        from_name = from_name or self.default_from_name
        from_field = f"{from_name} <{from_email}>" if from_name else from_email

        payload = {
            "from": from_field,
            "to": [to_email],
            "subject": subject,
            "text": body_text,
        }
        if body_html:
            payload["html"] = body_html
        if reply_to:
            payload["reply_to"] = reply_to
        unsub_headers = _unsubscribe_headers(to_email, from_email)
        if unsub_headers:
            payload["headers"] = unsub_headers

        try:
            resp = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
        except requests.RequestException as e:
            raise TransportError(f"Resend network error: {e}") from e

        if resp.status_code >= 400:
            # Resend's error body usually contains {message, name, statusCode}
            raise TransportError(
                f"Resend HTTP {resp.status_code}: {resp.text[:200]}"
            )

        body = resp.json()
        return {
            "transport": self.name,
            "provider_id": body.get("id", ""),
            "raw": body,
        }


class SmtpTransport:
    """Generic SMTP sender — works with Gmail, Google Workspace, Outlook,
    Mailgun SMTP, SendGrid SMTP, custom relays, etc.

    For Gmail/Workspace: requires a 16-char App Password (Google account
    must have 2FA enabled). https://myaccount.google.com/apppasswords
    Use SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USE_TLS=true.

    Sends from the SMTP_USER address by default (so deliverability uses
    that account's existing reputation — perfect for cold outreach where
    you don't want to set up DKIM/SPF separately).
    """

    name = "smtp"

    def __init__(self):
        self.host = os.getenv("SMTP_HOST", "").strip()
        port_str = os.getenv("SMTP_PORT", "587").strip() or "587"
        try:
            self.port = int(port_str)
        except ValueError:
            self.port = 587
        self.user = os.getenv("SMTP_USER", "").strip()
        self.password = os.getenv("SMTP_PASSWORD", "").strip()
        # TLS (STARTTLS) on port 587; SSL on port 465.
        # Default true; explicitly opt-out only when sending through a
        # plaintext relay (rare).
        use_tls_str = os.getenv("SMTP_USE_TLS", "true").strip().lower()
        self.use_tls = use_tls_str not in ("0", "false", "no", "off")
        self.use_ssl = self.port == 465

        self.default_from_email = (
            os.getenv("CWSCRAPER_FROM_EMAIL", "").strip() or self.user
        )
        self.default_from_name = os.getenv("CWSCRAPER_FROM_NAME", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    def send(
        self,
        *,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str = "",
        from_email: str = "",
        from_name: str = "",
        reply_to: str = "",
    ) -> dict:
        if not self.configured:
            raise TransportError(
                "SMTP transport not configured. Set SMTP_HOST, SMTP_USER, "
                "SMTP_PASSWORD (and SMTP_PORT if not 587)."
            )

        from_email = from_email or self.default_from_email
        from_name = from_name or self.default_from_name
        if not from_email:
            raise TransportError(
                "No sender address. Set CWSCRAPER_FROM_EMAIL or SMTP_USER."
            )

        msg = EmailMessage()
        msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        # Stable Message-ID derived from the from-domain helps deliverability.
        domain = from_email.split("@", 1)[1] if "@" in from_email else "localhost"
        msg["Message-ID"] = make_msgid(domain=domain)
        for hdr_name, hdr_val in _unsubscribe_headers(to_email, from_email).items():
            msg[hdr_name] = hdr_val

        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")

        try:
            if self.use_ssl:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=20)
            with smtp:
                smtp.ehlo()
                if self.use_tls and not self.use_ssl:
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise TransportError(
                f"SMTP auth failed for {self.user}@{self.host}: {e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else e.smtp_error}. "
                "For Gmail/Workspace use a 16-char App Password (not your account password) — "
                "https://myaccount.google.com/apppasswords"
            ) from e
        except (smtplib.SMTPException, OSError) as e:
            raise TransportError(f"SMTP {type(e).__name__}: {e}") from e

        return {
            "transport": self.name,
            "provider_id": msg["Message-ID"],
            "raw": {"host": self.host, "port": self.port},
        }


def get_transport() -> EmailTransport | None:
    """Return the configured transport, or None if no transport is configured.

    Selection precedence:
      1. Explicit EMAIL_TRANSPORT=smtp|resend env var
      2. SMTP if SMTP_HOST/USER/PASSWORD are all set
      3. Resend if RESEND_API_KEY + CWSCRAPER_FROM_EMAIL are set
      4. None
    """
    explicit = os.getenv("EMAIL_TRANSPORT", "").strip().lower()
    if explicit == "smtp":
        return SmtpTransport()
    if explicit == "resend":
        return ResendTransport()
    # Auto-detect — prefer SMTP because it's more often the user's
    # intentional choice (Gmail App Password) when both are set.
    smtp = SmtpTransport()
    if smtp.configured:
        return smtp
    resend = ResendTransport()
    if resend.configured:
        return resend
    return None
