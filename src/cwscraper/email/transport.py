"""Pluggable email transports.

v1 ships Resend only (3k/mo free, easiest auth). Postmark + SMTP are
roughed-in for later as classes implementing the same EmailTransport
protocol — the rest of the system only sees the abstract interface.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol

import requests

logger = logging.getLogger("cwscraper.email")


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


def get_transport() -> EmailTransport | None:
    """Return the configured transport, or None if no transport is configured.

    Selection order: explicit EMAIL_TRANSPORT env var, then Resend if its
    key is set. Future: Postmark, SMTP fallback.
    """
    explicit = os.getenv("EMAIL_TRANSPORT", "").strip().lower()
    if explicit == "resend":
        return ResendTransport()
    # Auto-detect: any configured transport wins
    resend = ResendTransport()
    if resend.configured:
        return resend
    return None
