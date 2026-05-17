"""Tests for the minimal email scheduler: transport, queue, dispatcher.

No real Resend calls — transport is mocked. Dispatcher's prospect-update
side effect is verified against a temp repo.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cwscraper.core.models import BusinessLead
from cwscraper.core.store import JSONRepository
from cwscraper.email.dispatcher import EmailDispatcher
from cwscraper.email.queue import ScheduledEmailQueue
from cwscraper.email.transport import (
    ResendTransport,
    SmtpTransport,
    TransportError,
    get_transport,
)


# ---------- Transport -----------------------------------------------------

def test_resend_unconfigured_when_no_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("CWSCRAPER_FROM_EMAIL", raising=False)
    t = ResendTransport()
    assert t.configured is False


def test_resend_configured_with_key_and_from(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_xxx")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "shaun@checkwellcall.com")
    t = ResendTransport()
    assert t.configured is True


def test_resend_send_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("CWSCRAPER_FROM_EMAIL", raising=False)
    with pytest.raises(TransportError, match="not configured"):
        ResendTransport().send(
            to_email="a@b.com", subject="x", body_text="y",
        )


def test_resend_send_posts_expected_payload(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "shaun@cwc.com")
    monkeypatch.setenv("CWSCRAPER_FROM_NAME", "Shaun")
    t = ResendTransport()

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"id": "resend_msg_123"}

    with patch("cwscraper.email.transport.requests.post", return_value=fake_resp) as mock_post:
        result = t.send(
            to_email="janet@acme.example",
            subject="Hi Janet",
            body_text="Body here",
            reply_to="shaun@cwc.com",
        )

    assert result["provider_id"] == "resend_msg_123"
    assert result["transport"] == "resend"

    # Inspect the actual request
    call = mock_post.call_args
    assert call.kwargs["json"]["from"] == "Shaun <shaun@cwc.com>"
    assert call.kwargs["json"]["to"] == ["janet@acme.example"]
    assert call.kwargs["json"]["subject"] == "Hi Janet"
    assert call.kwargs["json"]["text"] == "Body here"
    assert call.kwargs["json"]["reply_to"] == "shaun@cwc.com"
    assert call.kwargs["headers"]["Authorization"] == "Bearer re_test"


def test_resend_send_raises_on_http_error(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "shaun@cwc.com")

    fake_resp = MagicMock()
    fake_resp.status_code = 422
    fake_resp.text = '{"message": "Invalid sender domain"}'

    with patch("cwscraper.email.transport.requests.post", return_value=fake_resp):
        with pytest.raises(TransportError, match="HTTP 422"):
            ResendTransport().send(to_email="a@b.com", subject="x", body_text="y")


def test_get_transport_returns_resend_when_configured(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TRANSPORT"):
        monkeypatch.delenv(var, raising=False)
    t = get_transport()
    assert t is not None
    assert t.name == "resend"


def test_get_transport_returns_none_when_nothing_configured(monkeypatch):
    for var in (
        "RESEND_API_KEY", "CWSCRAPER_FROM_EMAIL", "EMAIL_TRANSPORT",
        "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    assert get_transport() is None


# ---------- SMTP transport (Gmail-friendly) -------------------------------

def _clear_email_env(monkeypatch):
    for var in (
        "EMAIL_TRANSPORT",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_USE_TLS",
        "RESEND_API_KEY", "CWSCRAPER_FROM_EMAIL", "CWSCRAPER_FROM_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_smtp_unconfigured_when_missing_pieces(monkeypatch):
    _clear_email_env(monkeypatch)
    assert SmtpTransport().configured is False
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    assert SmtpTransport().configured is False  # still missing user/password
    monkeypatch.setenv("SMTP_USER", "shaun@sgiq.ai")
    assert SmtpTransport().configured is False
    monkeypatch.setenv("SMTP_PASSWORD", "app-pwd-16chars1")
    assert SmtpTransport().configured is True


def test_smtp_send_with_gmail_settings(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "shaun@sgiq.ai")
    monkeypatch.setenv("SMTP_PASSWORD", "16-char-app-pwd!")
    monkeypatch.setenv("CWSCRAPER_FROM_NAME", "Shaun")

    fake_smtp = MagicMock()
    fake_smtp.__enter__ = MagicMock(return_value=fake_smtp)
    fake_smtp.__exit__ = MagicMock(return_value=False)

    with patch("cwscraper.email.transport.smtplib.SMTP", return_value=fake_smtp) as ctor:
        result = SmtpTransport().send(
            to_email="janet@homecare.example",
            subject="Hi Janet",
            body_text="Hello",
            reply_to="shaun@sgiq.ai",
        )

    ctor.assert_called_once_with("smtp.gmail.com", 587, timeout=20)
    # STARTTLS path on port 587
    fake_smtp.starttls.assert_called()
    fake_smtp.login.assert_called_once_with("shaun@sgiq.ai", "16-char-app-pwd!")
    fake_smtp.send_message.assert_called_once()

    # Verify the EmailMessage we sent has the right shape
    sent_msg = fake_smtp.send_message.call_args[0][0]
    assert sent_msg["To"] == "janet@homecare.example"
    assert sent_msg["Subject"] == "Hi Janet"
    assert "shaun@sgiq.ai" in sent_msg["From"]
    assert sent_msg["Reply-To"] == "shaun@sgiq.ai"

    assert result["transport"] == "smtp"
    assert result["provider_id"].startswith("<")  # Message-ID format


def test_smtp_uses_ssl_on_port_465(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "p")

    fake_smtp = MagicMock()
    fake_smtp.__enter__ = MagicMock(return_value=fake_smtp)
    fake_smtp.__exit__ = MagicMock(return_value=False)

    with patch("cwscraper.email.transport.smtplib.SMTP_SSL", return_value=fake_smtp) as ssl_ctor:
        with patch("cwscraper.email.transport.smtplib.SMTP") as plain_ctor:
            SmtpTransport().send(
                to_email="x@y.com", subject="s", body_text="b",
            )

    ssl_ctor.assert_called_once_with("smtp.example.com", 465, timeout=20)
    plain_ctor.assert_not_called()
    fake_smtp.starttls.assert_not_called()  # SSL doesn't STARTTLS


def test_smtp_auth_error_has_helpful_message(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "shaun@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "wrong-password")

    import smtplib as _smtplib
    fake_smtp = MagicMock()
    fake_smtp.__enter__ = MagicMock(return_value=fake_smtp)
    fake_smtp.__exit__ = MagicMock(return_value=False)
    fake_smtp.login.side_effect = _smtplib.SMTPAuthenticationError(
        535, b"Username and Password not accepted"
    )

    with patch("cwscraper.email.transport.smtplib.SMTP", return_value=fake_smtp):
        with pytest.raises(TransportError, match="App Password"):
            SmtpTransport().send(to_email="x@y.com", subject="s", body_text="b")


def test_get_transport_prefers_smtp_when_both_configured(monkeypatch):
    _clear_email_env(monkeypatch)
    # Set both
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "x@y.com")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")

    t = get_transport()
    assert t is not None
    assert t.name == "smtp"


def test_get_transport_honors_explicit_resend_override(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("EMAIL_TRANSPORT", "resend")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "x@y.com")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")

    t = get_transport()
    assert t is not None
    assert t.name == "resend"


# ---------- Queue ---------------------------------------------------------

@pytest.fixture
def queue(tmp_path: Path) -> ScheduledEmailQueue:
    return ScheduledEmailQueue(tmp_path)


def test_enqueue_creates_pending_entry(queue):
    entry = queue.enqueue(
        prospect_id="biz1", lead_type="business",
        to_email="owner@x.com", subject="Hi", body="Hello",
        scheduled_for=datetime.now(timezone.utc).isoformat(),
    )
    assert entry["status"] == "pending"
    assert entry["id"]
    assert entry["created_at"]
    assert queue.get(entry["id"])["status"] == "pending"


def test_due_pending_returns_only_past(queue):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past_entry = queue.enqueue(
        prospect_id="b1", lead_type="business", to_email="a@b.com",
        subject="s", body="b", scheduled_for=past,
    )
    queue.enqueue(
        prospect_id="b2", lead_type="business", to_email="c@d.com",
        subject="s", body="b", scheduled_for=future,
    )
    due = queue.due_pending()
    assert len(due) == 1
    assert due[0]["id"] == past_entry["id"]


def test_mark_sent_updates_status_and_provider_id(queue):
    entry = queue.enqueue(
        prospect_id="b1", lead_type="business", to_email="a@b.com",
        subject="s", body="b", scheduled_for="2026-01-01T00:00:00+00:00",
    )
    queue.mark_sent(entry["id"], provider_id="provider_xyz")
    updated = queue.get(entry["id"])
    assert updated["status"] == "sent"
    assert updated["provider_id"] == "provider_xyz"
    assert updated["sent_at"]


def test_mark_failed_records_error(queue):
    entry = queue.enqueue(
        prospect_id="b1", lead_type="business", to_email="a@b.com",
        subject="s", body="b", scheduled_for="2026-01-01T00:00:00+00:00",
    )
    queue.mark_failed(entry["id"], "boom")
    updated = queue.get(entry["id"])
    assert updated["status"] == "failed"
    assert updated["error"] == "boom"


def test_cancel_only_works_on_pending(queue):
    entry = queue.enqueue(
        prospect_id="b1", lead_type="business", to_email="a@b.com",
        subject="s", body="b", scheduled_for="2026-01-01T00:00:00+00:00",
    )
    assert queue.cancel(entry["id"]) is not None
    assert queue.get(entry["id"])["status"] == "cancelled"
    # Second cancel is a no-op (already terminal)
    assert queue.cancel(entry["id"]) is None


def test_list_filters_by_status_and_prospect(queue):
    queue.enqueue(prospect_id="b1", lead_type="business", to_email="a@b.com",
                  subject="s", body="b", scheduled_for="2026-01-01T00:00:00+00:00")
    queue.enqueue(prospect_id="b2", lead_type="business", to_email="c@d.com",
                  subject="s", body="b", scheduled_for="2026-01-02T00:00:00+00:00")
    assert len(queue.list(prospect_id="b1")) == 1
    assert len(queue.list(prospect_id="b2")) == 1
    assert len(queue.list(status="pending")) == 2


# ---------- Dispatcher ---------------------------------------------------

@pytest.fixture
def repo_with_biz(tmp_path: Path) -> JSONRepository:
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([BusinessLead(
        id="biz_for_email", name="Acme",
        email="owner@acme.example", pipeline_stage="qualified",
    )])
    return repo


def test_dispatcher_sends_due_emails_and_marks_sent(repo_with_biz, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")

    queue = ScheduledEmailQueue(tmp_path)
    dispatcher = EmailDispatcher(queue, repo_with_biz)

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    entry = queue.enqueue(
        prospect_id="biz_for_email", lead_type="business",
        to_email="owner@acme.example", subject="Hi", body="Hello",
        scheduled_for=past,
    )

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"id": "msg_abc"}

    with patch("cwscraper.email.transport.requests.post", return_value=fake_resp):
        sent = dispatcher.tick()

    assert sent == 1
    assert queue.get(entry["id"])["status"] == "sent"
    assert queue.get(entry["id"])["provider_id"] == "msg_abc"


def test_dispatcher_advances_pipeline_on_send(repo_with_biz, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")

    queue = ScheduledEmailQueue(tmp_path)
    dispatcher = EmailDispatcher(queue, repo_with_biz)

    queue.enqueue(
        prospect_id="biz_for_email", lead_type="business",
        to_email="owner@acme.example", subject="Hi", body="Hello",
        scheduled_for=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"id": "msg_x"}
    with patch("cwscraper.email.transport.requests.post", return_value=fake_resp):
        dispatcher.tick()

    biz = repo_with_biz.get_businesses()[0]
    assert biz["pipeline_stage"] == "outreach_sent"
    # Activity log gets an email_sent entry
    actions = [a["action"] for a in biz.get("activity_log", [])]
    assert "email_sent" in actions


def test_dispatcher_marks_failed_when_no_transport(repo_with_biz, tmp_path, monkeypatch):
    """If no transport is configured, due emails get marked failed (not silently dropped)."""
    for var in ("RESEND_API_KEY", "CWSCRAPER_FROM_EMAIL", "EMAIL_TRANSPORT"):
        monkeypatch.delenv(var, raising=False)

    queue = ScheduledEmailQueue(tmp_path)
    dispatcher = EmailDispatcher(queue, repo_with_biz)
    entry = queue.enqueue(
        prospect_id="biz_for_email", lead_type="business",
        to_email="owner@acme.example", subject="Hi", body="Hello",
        scheduled_for=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    sent = dispatcher.tick()
    assert sent == 0
    failed = queue.get(entry["id"])
    assert failed["status"] == "failed"
    assert "No email transport configured" in failed["error"]


def test_dispatcher_marks_failed_when_transport_errors(repo_with_biz, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")

    queue = ScheduledEmailQueue(tmp_path)
    dispatcher = EmailDispatcher(queue, repo_with_biz)
    entry = queue.enqueue(
        prospect_id="biz_for_email", lead_type="business",
        to_email="owner@acme.example", subject="Hi", body="Hello",
        scheduled_for=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    bad_resp = MagicMock(status_code=500)
    bad_resp.text = "Resend down"
    with patch("cwscraper.email.transport.requests.post", return_value=bad_resp):
        dispatcher.tick()

    failed = queue.get(entry["id"])
    assert failed["status"] == "failed"
    assert "HTTP 500" in failed["error"]


def test_dispatcher_does_not_advance_pipeline_past_terminal(tmp_path, monkeypatch):
    """A 'customer' or 'lost' prospect should NOT get bumped back to outreach_sent."""
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")

    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([BusinessLead(
        id="biz_c", name="Customer Co", email="c@x.com", pipeline_stage="customer",
    )])
    queue = ScheduledEmailQueue(tmp_path)
    dispatcher = EmailDispatcher(queue, repo)
    queue.enqueue(
        prospect_id="biz_c", lead_type="business",
        to_email="c@x.com", subject="thanks", body="follow-up",
        scheduled_for=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"id": "x"}
    with patch("cwscraper.email.transport.requests.post", return_value=fake_resp):
        dispatcher.tick()

    assert repo.get_businesses()[0]["pipeline_stage"] == "customer"  # unchanged
