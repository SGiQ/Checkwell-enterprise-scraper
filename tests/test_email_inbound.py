"""Tests for inbound IMAP polling: classifier, body extraction, prospect updates.

Real IMAP isn't dialed — ImapClient is injected as a MagicMock that returns
canned RFC-822 message bytes.
"""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cwscraper.core.models import BusinessLead
from cwscraper.core.store import JSONRepository
from cwscraper.email.inbound import (
    ImapClient,
    InboundEmailPoller,
    classify_reply,
)
from cwscraper.email.suppression import SuppressionList


# ---------- Classifier ----------------------------------------------------

@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("Re: Outreach", "Please unsubscribe me", "unsubscribe"),
        ("Re: Outreach", "Take me off your list", "unsubscribe"),
        ("Out of office", "I am on vacation until Friday", "out_of_office"),
        ("Re: Quick intro", "Auto-reply: I'm away from my desk", "out_of_office"),
        ("Re: Quick intro", "Not interested, thanks", "not_interested"),
        ("Re: Quick intro", "We're all set, no thank you", "not_interested"),
        ("Re: Quick intro", "Sounds good — let's chat next week", "interested"),
        ("Re: Quick intro", "Yes, tell me more about pricing", "interested"),
        ("Re: Quick intro", "Happy to schedule a call", "interested"),
        ("Re: Quick intro", "Thanks for reaching out!", "unclear"),
    ],
)
def test_classify_reply_labels(subject, body, expected):
    assert classify_reply(from_address="janet@x.com", subject=subject, body=body) == expected


def test_classify_reply_bounce_from_address():
    assert classify_reply(
        from_address="mailer-daemon@mail.googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body="Address not found",
    ) == "bounce"


def test_classify_reply_bounce_subject():
    """Even a regular-looking from-address gets caught when subject screams bounce."""
    assert classify_reply(
        from_address="postmaster@example.com",
        subject="Undeliverable: your message",
        body="",
    ) == "bounce"


def test_classify_reply_ordering_unsubscribe_beats_ooo():
    """If a message contains both OOO and unsubscribe markers, unsubscribe wins
    because it carries legal obligation."""
    assert classify_reply(
        from_address="x@y.com",
        subject="Auto-reply",
        body="I'm on vacation. Please unsubscribe me from this list.",
    ) == "unsubscribe"


# ---------- Inbound poller tick ------------------------------------------

@pytest.fixture
def repo(tmp_path: Path) -> JSONRepository:
    return JSONRepository(tmp_path)


@pytest.fixture
def suppression(tmp_path: Path) -> SuppressionList:
    return SuppressionList(tmp_path)


@pytest.fixture
def seeded_repo(repo: JSONRepository) -> JSONRepository:
    """Seed one business prospect we can match against in tests."""
    biz = BusinessLead(
        id="biz1",
        source="google_places",
        name="Sunset Seniors",
        city="Tucson",
        state="AZ",
        email="info@sunsetseniors.example",
        pipeline_stage="outreach_sent",
    )
    repo.add_businesses([biz])
    return repo


def _raw_email(*, from_addr: str, subject: str, body: str) -> bytes:
    """Build minimal RFC-822 bytes — what ImapClient.fetch_unseen returns."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "hello@checkwellcall.co"
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def _client_returning(*messages: bytes) -> MagicMock:
    """Build a stub ImapClient whose fetch_unseen returns the given messages."""
    client = MagicMock(spec=ImapClient)
    client.fetch_unseen.return_value = list(messages)
    return client


def test_tick_skips_when_imap_unconfigured(monkeypatch, seeded_repo, suppression):
    for var in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    poller = InboundEmailPoller(seeded_repo, suppression)
    result = poller.tick()
    assert result == {"fetched": 0, "skipped_unconfigured": True}


def test_tick_classifies_unsubscribe_and_adds_suppression(monkeypatch, seeded_repo, suppression):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    raw = _raw_email(
        from_addr="info@sunsetseniors.example",
        subject="Re: hello",
        body="Please unsubscribe me.",
    )
    client = _client_returning(raw)
    poller = InboundEmailPoller(seeded_repo, suppression)

    result = poller.tick(client=client)

    assert result["fetched"] == 1
    assert result["classified"] == {"unsubscribe": 1}
    assert result["matched"] == 1
    # Suppression captured the sender.
    assert suppression.is_suppressed("info@sunsetseniors.example")
    # Prospect moved to 'lost'.
    biz = next(p for p in seeded_repo.get_all_prospects() if p["id"] == "biz1")
    assert biz["pipeline_stage"] == "lost"
    actions = [e["action"] for e in biz["activity_log"]]
    assert "reply_unsubscribe" in actions


def test_tick_interested_moves_prospect_to_reply_received(monkeypatch, seeded_repo, suppression):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    raw = _raw_email(
        from_addr="info@sunsetseniors.example",
        subject="Re: hello",
        body="Interested — let's schedule a call this week.",
    )
    client = _client_returning(raw)
    poller = InboundEmailPoller(seeded_repo, suppression)

    poller.tick(client=client)

    biz = next(p for p in seeded_repo.get_all_prospects() if p["id"] == "biz1")
    assert biz["pipeline_stage"] == "reply_received"
    assert not suppression.is_suppressed("info@sunsetseniors.example")


def test_tick_does_not_downgrade_already_advanced_prospect(monkeypatch, repo, suppression):
    """An 'interested' reply shouldn't pull a prospect back from 'meeting_booked'."""
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    biz = BusinessLead(
        id="biz_meet", source="google_places", name="X", city="T", state="AZ",
        email="boss@x.example", pipeline_stage="meeting_booked",
    )
    repo.add_businesses([biz])
    raw = _raw_email(from_addr="boss@x.example", subject="Re: hi", body="Sounds good!")
    poller = InboundEmailPoller(repo, suppression)

    poller.tick(client=_client_returning(raw))

    final = next(p for p in repo.get_all_prospects() if p["id"] == "biz_meet")
    assert final["pipeline_stage"] == "meeting_booked"
    actions = [e["action"] for e in final["activity_log"]]
    assert "reply_interested" in actions


def test_tick_bounce_adds_suppression_no_stage_change(monkeypatch, seeded_repo, suppression):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    raw = _raw_email(
        from_addr="mailer-daemon@google.com",
        subject="Delivery Status Notification (Failure)",
        body="Address not found",
    )
    client = _client_returning(raw)
    poller = InboundEmailPoller(seeded_repo, suppression)

    poller.tick(client=client)

    assert suppression.is_suppressed("mailer-daemon@google.com")
    biz = next(p for p in seeded_repo.get_all_prospects() if p["id"] == "biz1")
    # Bounce came from an unrelated address; biz1 stage shouldn't move.
    assert biz["pipeline_stage"] == "outreach_sent"


def test_tick_unmatched_unsubscribe_still_suppressed(monkeypatch, repo, suppression):
    """A reply from someone we have no prospect for still gets suppressed
    (we never want to email them again, prospect or not)."""
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    raw = _raw_email(
        from_addr="random@somewhere.example",
        subject="stop",
        body="Please remove me from your list.",
    )
    poller = InboundEmailPoller(repo, suppression)
    result = poller.tick(client=_client_returning(raw))

    assert result["unmatched"] == 1
    assert suppression.is_suppressed("random@somewhere.example")


def test_tick_matches_via_contacts_list(monkeypatch, repo, suppression):
    """Reply from a contact-level email (not business.email) should still match."""
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    biz = BusinessLead(
        id="biz_c", source="google_places", name="X", city="T", state="AZ",
        email="general@x.example", pipeline_stage="outreach_sent",
        contacts=[{"name": "Janet", "email": "janet@x.example"}],
    )
    repo.add_businesses([biz])
    raw = _raw_email(from_addr="janet@x.example", subject="Re: hi",
                     body="Tell me more please")
    poller = InboundEmailPoller(repo, suppression)

    poller.tick(client=_client_returning(raw))

    final = next(p for p in repo.get_all_prospects() if p["id"] == "biz_c")
    assert final["pipeline_stage"] == "reply_received"
