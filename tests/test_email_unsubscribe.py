"""Tests for the public /unsubscribe route + the List-Unsubscribe headers
the transports inject when CWSCRAPER_UNSUBSCRIBE_URL is set.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cwscraper.email.transport import ResendTransport, SmtpTransport


# ---------- Transport: List-Unsubscribe headers --------------------------

def _clear_email_env(monkeypatch):
    for var in (
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_USE_TLS",
        "RESEND_API_KEY", "CWSCRAPER_FROM_EMAIL", "CWSCRAPER_FROM_NAME",
        "EMAIL_TRANSPORT", "CWSCRAPER_UNSUBSCRIBE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_smtp_omits_list_unsubscribe_when_env_not_set(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "p")

    fake_smtp = MagicMock()
    fake_smtp.__enter__ = MagicMock(return_value=fake_smtp)
    fake_smtp.__exit__ = MagicMock(return_value=False)

    with patch("cwscraper.email.transport.smtplib.SMTP", return_value=fake_smtp):
        SmtpTransport().send(
            to_email="janet@x.com", subject="Hi", body_text="Hello",
        )

    sent = fake_smtp.send_message.call_args[0][0]
    assert sent.get("List-Unsubscribe") is None


def test_smtp_includes_list_unsubscribe_when_env_set(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "shaun@checkwellcall.co")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("CWSCRAPER_UNSUBSCRIBE_URL", "https://checkwellcall.com/unsubscribe")

    fake_smtp = MagicMock()
    fake_smtp.__enter__ = MagicMock(return_value=fake_smtp)
    fake_smtp.__exit__ = MagicMock(return_value=False)

    with patch("cwscraper.email.transport.smtplib.SMTP", return_value=fake_smtp):
        SmtpTransport().send(
            to_email="janet@x.com", subject="Hi", body_text="Hello",
        )

    sent = fake_smtp.send_message.call_args[0][0]
    assert "checkwellcall.com/unsubscribe" in sent["List-Unsubscribe"]
    assert "janet%40x.com" in sent["List-Unsubscribe"]  # URL-encoded
    assert sent["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_resend_payload_includes_headers_when_env_set(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "shaun@cwc.com")
    monkeypatch.setenv("CWSCRAPER_UNSUBSCRIBE_URL", "https://checkwellcall.com/unsubscribe")

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"id": "msg_1"}

    with patch("cwscraper.email.transport.requests.post", return_value=fake_resp) as mock_post:
        ResendTransport().send(
            to_email="janet@x.com", subject="Hi", body_text="Hello",
        )

    payload = mock_post.call_args.kwargs["json"]
    assert "headers" in payload
    assert "checkwellcall.com/unsubscribe" in payload["headers"]["List-Unsubscribe"]


def test_resend_payload_omits_headers_when_env_not_set(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "shaun@cwc.com")

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"id": "msg_1"}

    with patch("cwscraper.email.transport.requests.post", return_value=fake_resp) as mock_post:
        ResendTransport().send(
            to_email="janet@x.com", subject="Hi", body_text="Hello",
        )

    payload = mock_post.call_args.kwargs["json"]
    assert "headers" not in payload


# ---------- Public /unsubscribe route ------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """Bring up the Flask app pointed at a tmp data dir, return a test client."""
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_NICHE", "caregiver")
    # No background threads needed for HTTP tests — they no-op when transports
    # aren't configured. Just clear any leftover SMTP/IMAP env.
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
                "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD",
                "RESEND_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from cwscraper.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), app


def test_unsubscribe_get_renders_confirmation(client):
    test_client, app = client
    resp = test_client.get("/unsubscribe?email=janet@x.com")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "unsubscribe" in body.lower()
    assert "janet@x.com" in body
    # Should have suppressed the address.
    suppression = app.extensions["cwscraper"].suppression
    assert suppression.is_suppressed("janet@x.com")


def test_unsubscribe_get_without_email_still_returns_200(client):
    test_client, _ = client
    resp = test_client.get("/unsubscribe")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # Generic accept-the-request page, doesn't reveal whether we know them.
    assert "received" in body.lower() or "unsubscribe" in body.lower()


def test_unsubscribe_post_one_click(client):
    test_client, app = client
    resp = test_client.post(
        "/unsubscribe",
        data={"email": "owner@example.com", "List-Unsubscribe": "One-Click"},
    )
    assert resp.status_code == 200
    assert resp.json == {"ok": True}
    suppression = app.extensions["cwscraper"].suppression
    assert suppression.is_suppressed("owner@example.com")


def test_unsubscribe_post_via_query_string(client):
    """Some mail clients preserve the ?email= from the List-Unsubscribe URL
    rather than putting it in the body — our route should accept both."""
    test_client, app = client
    resp = test_client.post("/unsubscribe?email=owner@example.com")
    assert resp.status_code == 200
    assert app.extensions["cwscraper"].suppression.is_suppressed("owner@example.com")
