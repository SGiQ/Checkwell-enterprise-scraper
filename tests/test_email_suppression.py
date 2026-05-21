"""Tests for the suppression list + dispatcher's suppression check."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cwscraper.core.store import JSONRepository
from cwscraper.email.dispatcher import EmailDispatcher
from cwscraper.email.queue import ScheduledEmailQueue
from cwscraper.email.suppression import SuppressionList


@pytest.fixture
def supp(tmp_path: Path) -> SuppressionList:
    return SuppressionList(tmp_path)


# ---------- SuppressionList ----------------------------------------------

def test_empty_list_returns_false_for_known_address(supp):
    assert supp.is_suppressed("a@b.com") is False


def test_empty_address_always_suppressed(supp):
    """Defensive: an empty 'to' should never get a send attempt."""
    assert supp.is_suppressed("") is True
    assert supp.is_suppressed(None) is True


def test_add_then_is_suppressed(supp):
    supp.add("Owner@Example.com", reason="unsubscribe")
    assert supp.is_suppressed("owner@example.com") is True
    assert supp.is_suppressed("OWNER@EXAMPLE.COM") is True  # case-insensitive


def test_add_normalizes_and_dedupes(supp):
    supp.add("  Janet@X.com  ", reason="unsubscribe")
    supp.add("janet@x.com", reason="bounce", notes="second time")  # should update, not duplicate
    rows = supp.list_all()
    assert len(rows) == 1
    assert rows[0]["email"] == "janet@x.com"
    assert rows[0]["reason"] == "bounce"
    assert rows[0]["notes"] == "second time"


def test_add_rejects_invalid_email(supp):
    assert supp.add("") is None
    assert supp.add("not-an-email") is None
    assert supp.list_all() == []


def test_add_unknown_reason_coerced_to_manual(supp):
    entry = supp.add("a@b.com", reason="bogus-reason")
    assert entry["reason"] == "manual"


def test_remove_returns_true_when_found(supp):
    supp.add("a@b.com", reason="manual")
    assert supp.remove("A@B.COM") is True
    assert supp.is_suppressed("a@b.com") is False


def test_remove_returns_false_when_missing(supp):
    assert supp.remove("unknown@x.com") is False


def test_list_all_newest_first(supp):
    supp.add("first@x.com", reason="manual")
    supp.add("second@x.com", reason="manual")
    rows = supp.list_all()
    assert [r["email"] for r in rows] == ["second@x.com", "first@x.com"]


# ---------- Dispatcher honors suppression --------------------------------

def test_dispatcher_skips_suppressed_recipient(tmp_path: Path, monkeypatch):
    """End-to-end-ish: a queued email to a suppressed address gets marked
    failed without invoking the transport."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    for var in ("RESEND_API_KEY", "EMAIL_TRANSPORT"):
        monkeypatch.delenv(var, raising=False)

    repo = JSONRepository(tmp_path)
    queue = ScheduledEmailQueue(tmp_path)
    suppression = SuppressionList(tmp_path)
    suppression.add("blocked@example.com", reason="unsubscribe")

    past = (datetime.now(timezone.utc).isoformat())
    entry = queue.enqueue(
        prospect_id="biz1", lead_type="business",
        to_email="blocked@example.com",
        subject="Hi", body="Hello",
        scheduled_for=past,
    )

    dispatcher = EmailDispatcher(queue, repo, suppression=suppression)

    # If the transport were called we'd see SMTP attempted. Patch it out
    # to prove it isn't.
    with patch("cwscraper.email.dispatcher.get_transport") as gt:
        fake_transport = MagicMock()
        gt.return_value = fake_transport
        dispatcher.tick()
        fake_transport.send.assert_not_called()

    final = queue.get(entry["id"])
    assert final["status"] == "failed"
    assert "Suppressed" in (final["error"] or "")


def test_dispatcher_sends_normally_when_recipient_not_suppressed(tmp_path: Path, monkeypatch):
    """Sanity: the suppression-aware dispatcher still sends to clean addresses."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    for var in ("RESEND_API_KEY", "EMAIL_TRANSPORT"):
        monkeypatch.delenv(var, raising=False)

    repo = JSONRepository(tmp_path)
    queue = ScheduledEmailQueue(tmp_path)
    suppression = SuppressionList(tmp_path)
    # Suppress someone *else* — recipient is clean.
    suppression.add("other@example.com", reason="unsubscribe")

    past = datetime.now(timezone.utc).isoformat()
    entry = queue.enqueue(
        prospect_id="", lead_type="business",
        to_email="fresh@example.com",
        subject="Hi", body="Hello",
        scheduled_for=past,
    )

    dispatcher = EmailDispatcher(queue, repo, suppression=suppression)

    with patch("cwscraper.email.dispatcher.get_transport") as gt:
        fake_transport = MagicMock()
        fake_transport.send.return_value = {"transport": "smtp", "provider_id": "msg_1"}
        gt.return_value = fake_transport
        sent = dispatcher.tick()

    assert sent == 1
    assert queue.get(entry["id"])["status"] == "sent"
