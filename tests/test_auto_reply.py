"""Tests for the auto-reply drafter + inbound-drafts queue + the
inbound poller's integration with both.

The Anthropic SDK is mocked — no real API calls.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cwscraper.core.models import BusinessLead
from cwscraper.core.store import JSONRepository
from cwscraper.email.inbound import InboundEmailPoller
from cwscraper.email.inbound_drafts import InboundDraftQueue
from cwscraper.email.suppression import SuppressionList
from cwscraper.replies.auto_reply import (
    _trim_reply_for_prompt,
    draft_auto_reply,
)


# =========================================================================
# Helpers
# =========================================================================

def _mock_anthropic(text: str = "Thanks for the reply.\n\nLet's talk — book here: {link}\n\nBest, Shaun"):
    mock = MagicMock()
    mock.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=200, output_tokens=60,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    return mock


@pytest.fixture
def business():
    return {
        "id": "biz1",
        "name": "Sunshine Home Care",
        "city": "Sarasota", "state": "FL",
        "category": "home_health_agency",
        "email": "owner@sunshine.example",
    }


# =========================================================================
# draft_auto_reply()
# =========================================================================

def test_draft_uses_llm_when_configured(monkeypatch, business):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CWSCRAPER_BOOKING_LINK", "https://cal.com/foo")
    monkeypatch.setenv("CWSCRAPER_DEMO_VIDEO_LINK", "https://loom.com/bar")
    mock = _mock_anthropic("Great to hear back. Here are options: ...")

    result = draft_auto_reply(
        business=business,
        reply_text="Yes — sounds interesting. Tell me more.",
        reply_subject="Re: Wellness checks for Sarasota families",
        client=mock,
    )

    assert result.error is None
    assert result.body.startswith("Great to hear back")
    assert result.to_email == "owner@sunshine.example"
    assert result.business_name == "Sunshine Home Care"
    assert result.subject == "Re: Wellness checks for Sarasota families"
    assert result.booking_link == "https://cal.com/foo"
    assert result.demo_video_link == "https://loom.com/bar"
    mock.messages.create.assert_called_once()


def test_draft_adds_re_prefix_when_missing(business):
    mock = _mock_anthropic()
    result = draft_auto_reply(
        business=business,
        reply_text="ok",
        reply_subject="Wellness checks — no Re prefix",
        client=mock,
    )
    assert result.subject == "Re: Wellness checks — no Re prefix"


def test_draft_preserves_re_prefix_when_present(business):
    mock = _mock_anthropic()
    result = draft_auto_reply(
        business=business,
        reply_text="ok",
        reply_subject="Re: Already prefixed",
        client=mock,
    )
    assert result.subject == "Re: Already prefixed"


def test_draft_handles_empty_subject(business):
    mock = _mock_anthropic()
    result = draft_auto_reply(
        business=business, reply_text="ok", reply_subject="", client=mock,
    )
    assert result.subject == "Re: your reply"


def test_draft_strips_preamble_quotes(business, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CWSCRAPER_BOOKING_LINK", "https://cal.com/x")
    mock = _mock_anthropic('"Here\'s the reply: This is the actual body."')
    result = draft_auto_reply(
        business=business, reply_text="ok", reply_subject="Re: hi", client=mock,
    )
    assert "Here's the reply:" not in result.body
    assert not result.body.startswith('"')


def test_draft_falls_back_to_template_without_anthropic_key(monkeypatch, business):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CWSCRAPER_BOOKING_LINK", "https://cal.com/x")
    monkeypatch.setenv("CWSCRAPER_DEMO_VIDEO_LINK", "https://loom.com/y")

    result = draft_auto_reply(
        business=business,
        reply_text="Interested",
        reply_subject="Re: hi",
        client=None,
    )
    assert result.error is not None
    assert "ANTHROPIC_API_KEY" in result.error
    # Templated fallback still includes the booking + demo links
    assert "cal.com/x" in result.body
    assert "loom.com/y" in result.body
    assert "Sunshine Home Care" in result.body


def test_draft_falls_back_when_no_links_configured(monkeypatch, business):
    """No booking link AND no demo link → falls back to the simplest reply
    asking what would work best (no AI call needed)."""
    for var in ("CWSCRAPER_BOOKING_LINK", "CWSCRAPER_DEMO_VIDEO_LINK"):
        monkeypatch.delenv(var, raising=False)
    mock = _mock_anthropic("Should not be used.")
    result = draft_auto_reply(
        business=business, reply_text="ok", reply_subject="Re: hi", client=mock,
    )
    assert result.error is None
    # Skipped the LLM path entirely — the "what works best?" template is fine
    mock.messages.create.assert_not_called()
    assert "Sunshine Home Care" in result.body


def test_draft_falls_back_when_llm_raises(monkeypatch, business):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CWSCRAPER_BOOKING_LINK", "https://cal.com/x")

    mock = MagicMock()
    mock.messages.create.side_effect = RuntimeError("API down")
    result = draft_auto_reply(
        business=business, reply_text="ok", reply_subject="Re: hi", client=mock,
    )
    assert result.error is not None
    assert "LLM failed" in result.error
    assert "cal.com/x" in result.body


def test_draft_uses_contact_email_when_business_email_missing():
    biz = {
        "id": "biz2", "name": "X", "email": "",
        "contacts": [{"name": "Jane", "email": "jane@x.example"}],
    }
    mock = _mock_anthropic()
    result = draft_auto_reply(
        business=biz, reply_text="ok", reply_subject="Re: hi", client=mock,
    )
    assert result.to_email == "jane@x.example"


# =========================================================================
# _trim_reply_for_prompt
# =========================================================================

def test_trim_strips_quoted_reply_block():
    reply = """Yes, this looks interesting.

On Tue, May 21, Shaun wrote:
> Hi Jane,
> [long quoted original]
"""
    assert _trim_reply_for_prompt(reply) == "Yes, this looks interesting."


def test_trim_strips_bare_quote_prefix():
    reply = """Thanks!

> On 2026-05-21
> Hi there
"""
    assert _trim_reply_for_prompt(reply) == "Thanks!"


def test_trim_caps_at_max_chars():
    reply = "word " * 500  # ~2500 chars
    trimmed = _trim_reply_for_prompt(reply, max_chars=200)
    assert len(trimmed) <= 205   # 200 + ellipsis room
    assert trimmed.endswith("…")


def test_trim_empty_input():
    assert _trim_reply_for_prompt("") == ""
    assert _trim_reply_for_prompt(None) == ""


# =========================================================================
# InboundDraftQueue
# =========================================================================

@pytest.fixture
def draft_queue(tmp_path: Path) -> InboundDraftQueue:
    return InboundDraftQueue(tmp_path)


def _sample_draft(business_id: str = "biz1") -> dict:
    return {
        "business_id": business_id,
        "business_name": "Test Co",
        "to_email": "test@example.com",
        "subject": "Re: hi",
        "body": "Body text.",
        "original_reply_excerpt": "What they said.",
        "booking_link": "https://cal.com/x",
        "demo_video_link": "",
        "error": None,
    }


def test_queue_enqueue_persists_draft(draft_queue):
    entry = draft_queue.enqueue(_sample_draft())
    assert entry["id"]
    assert entry["status"] == "pending"
    assert entry["created_at"]
    fetched = draft_queue.get(entry["id"])
    assert fetched == entry


def test_queue_lists_pending_first(draft_queue):
    d1 = draft_queue.enqueue(_sample_draft(business_id="b1"))
    d2 = draft_queue.enqueue(_sample_draft(business_id="b2"))
    draft_queue.mark_approved(d1["id"], queue_id="qid_1")
    drafts = draft_queue.list()
    # Pending (d2) should come before the approved one
    assert drafts[0]["id"] == d2["id"]
    assert drafts[1]["id"] == d1["id"]


def test_queue_pending_for_business_returns_first_match(draft_queue):
    d = draft_queue.enqueue(_sample_draft(business_id="bizX"))
    found = draft_queue.pending_for_business("bizX")
    assert found is not None
    assert found["id"] == d["id"]


def test_queue_pending_for_business_ignores_decided(draft_queue):
    d = draft_queue.enqueue(_sample_draft(business_id="bizX"))
    draft_queue.mark_dismissed(d["id"])
    assert draft_queue.pending_for_business("bizX") is None


def test_queue_update_body(draft_queue):
    d = draft_queue.enqueue(_sample_draft())
    draft_queue.update_body(d["id"], subject="New subj", body="New body")
    updated = draft_queue.get(d["id"])
    assert updated["subject"] == "New subj"
    assert updated["body"] == "New body"


def test_queue_mark_approved_records_queue_id(draft_queue):
    d = draft_queue.enqueue(_sample_draft())
    draft_queue.mark_approved(d["id"], queue_id="qid_xyz")
    updated = draft_queue.get(d["id"])
    assert updated["status"] == "approved"
    assert updated["approved_queue_id"] == "qid_xyz"
    assert updated["decided_at"]


def test_queue_mark_dismissed(draft_queue):
    d = draft_queue.enqueue(_sample_draft())
    draft_queue.mark_dismissed(d["id"])
    updated = draft_queue.get(d["id"])
    assert updated["status"] == "dismissed"
    assert updated["decided_at"]


# =========================================================================
# Inbound poller integration — 'interested' triggers auto-draft
# =========================================================================

def _raw_email(*, from_addr: str, subject: str, body: str) -> bytes:
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def _imap_client_with(*raw_msgs):
    from cwscraper.email.inbound import ImapClient
    client = MagicMock(spec=ImapClient)
    client.fetch_unseen.return_value = list(raw_msgs)
    return client


@pytest.fixture
def seeded_repo(tmp_path: Path) -> JSONRepository:
    repo = JSONRepository(tmp_path)
    repo.add_businesses([
        BusinessLead(
            id="biz_int", source="google_places",
            name="Heartland PACE", city="Greenville", state="SC",
            category="pace_program", email="info@heartland.example",
            pipeline_stage="outreach_sent",
        ),
    ])
    return repo


def test_interested_reply_triggers_auto_draft(monkeypatch, tmp_path, seeded_repo):
    """Reply classified as 'interested' should populate the inbound-draft queue."""
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # use templated fallback
    monkeypatch.setenv("CWSCRAPER_BOOKING_LINK", "https://cal.com/x")

    suppression = SuppressionList(tmp_path)
    drafts_q = InboundDraftQueue(tmp_path)
    poller = InboundEmailPoller(seeded_repo, suppression, inbound_drafts=drafts_q)

    raw = _raw_email(
        from_addr="info@heartland.example",
        subject="Re: Referral partnership",
        body="Interested — let's talk this week.",
    )
    poller.tick(client=_imap_client_with(raw))

    pending = drafts_q.list(status="pending")
    assert len(pending) == 1
    d = pending[0]
    assert d["business_id"] == "biz_int"
    assert d["to_email"] == "info@heartland.example"
    assert d["subject"] == "Re: Referral partnership"
    assert "cal.com/x" in d["body"]
    # The original-reply excerpt is preserved for operator context
    assert "Interested" in d["original_reply_excerpt"]


def test_interested_reply_does_not_duplicate_draft(monkeypatch, tmp_path, seeded_repo):
    """Two replies from the same recipient before the first is reviewed
    should NOT produce two pending drafts — the second is consolidated
    into a no-op (the first draft is still pending)."""
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setenv("CWSCRAPER_BOOKING_LINK", "https://cal.com/x")

    suppression = SuppressionList(tmp_path)
    drafts_q = InboundDraftQueue(tmp_path)
    poller = InboundEmailPoller(seeded_repo, suppression, inbound_drafts=drafts_q)

    # First interested reply
    raw1 = _raw_email(
        from_addr="info@heartland.example",
        subject="Re: hi", body="Tell me more, sounds good",
    )
    poller.tick(client=_imap_client_with(raw1))
    # Second interested reply BEFORE review
    raw2 = _raw_email(
        from_addr="info@heartland.example",
        subject="Re: hi", body="Actually here's a thought — interested",
    )
    poller.tick(client=_imap_client_with(raw2))

    pending = drafts_q.list(status="pending")
    assert len(pending) == 1


def test_non_interested_replies_do_not_draft(monkeypatch, tmp_path, seeded_repo):
    """Only 'interested' verdicts trigger auto-drafts. Other classifications
    update the prospect but skip drafting."""
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")

    suppression = SuppressionList(tmp_path)
    drafts_q = InboundDraftQueue(tmp_path)
    poller = InboundEmailPoller(seeded_repo, suppression, inbound_drafts=drafts_q)

    raw = _raw_email(
        from_addr="info@heartland.example",
        subject="Re: hi",
        body="Not interested, please remove",  # classified as unsubscribe
    )
    poller.tick(client=_imap_client_with(raw))

    assert drafts_q.list(status="pending") == []


def test_poller_without_drafts_queue_still_works(monkeypatch, tmp_path, seeded_repo):
    """Backward compat: a poller constructed without the inbound_drafts arg
    should still process replies (just skip the auto-draft step)."""
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")

    suppression = SuppressionList(tmp_path)
    poller = InboundEmailPoller(seeded_repo, suppression)  # no inbound_drafts

    raw = _raw_email(
        from_addr="info@heartland.example",
        subject="Re: hi", body="Interested",
    )
    result = poller.tick(client=_imap_client_with(raw))
    assert result["matched"] == 1


# =========================================================================
# Routes
# =========================================================================

@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_NICHE", "pace_programs_se")
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
                "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD",
                "RESEND_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CWSCRAPER_BOOKING_LINK", "https://cal.com/x")
    from cwscraper.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), app


def test_route_list_empty(app_client):
    test_client, _ = app_client
    resp = test_client.get("/api/replies/inbound-drafts")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["drafts"] == []
    assert data["pending_count"] == 0


def test_route_approve_moves_to_send_queue(app_client):
    test_client, app = app_client
    ctx = app.extensions["cwscraper"]
    d = ctx.inbound_drafts.enqueue(_sample_draft())

    resp = test_client.post(f"/api/replies/inbound-drafts/{d['id']}/approve")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["queue_id"]
    assert body["scheduled_for"]

    # Draft marked approved
    updated = ctx.inbound_drafts.get(d["id"])
    assert updated["status"] == "approved"
    assert updated["approved_queue_id"] == body["queue_id"]

    # Email landed in the scheduled-emails queue
    queue_entry = ctx.email_queue.get(body["queue_id"])
    assert queue_entry is not None
    assert queue_entry["to_email"] == "test@example.com"
    assert queue_entry["body"] == "Body text."


def test_route_approve_rejects_already_decided(app_client):
    test_client, app = app_client
    ctx = app.extensions["cwscraper"]
    d = ctx.inbound_drafts.enqueue(_sample_draft())
    ctx.inbound_drafts.mark_dismissed(d["id"])
    resp = test_client.post(f"/api/replies/inbound-drafts/{d['id']}/approve")
    assert resp.status_code == 400
    assert "already decided" in resp.get_json()["error"]


def test_route_edit_updates_subject_and_body(app_client):
    test_client, app = app_client
    ctx = app.extensions["cwscraper"]
    d = ctx.inbound_drafts.enqueue(_sample_draft())

    resp = test_client.post(
        f"/api/replies/inbound-drafts/{d['id']}/edit",
        json={"subject": "Edited subject", "body": "Edited body."},
    )
    assert resp.status_code == 200
    updated = ctx.inbound_drafts.get(d["id"])
    assert updated["subject"] == "Edited subject"
    assert updated["body"] == "Edited body."


def test_route_dismiss(app_client):
    test_client, app = app_client
    ctx = app.extensions["cwscraper"]
    d = ctx.inbound_drafts.enqueue(_sample_draft())
    resp = test_client.post(f"/api/replies/inbound-drafts/{d['id']}/dismiss")
    assert resp.status_code == 200
    updated = ctx.inbound_drafts.get(d["id"])
    assert updated["status"] == "dismissed"


def test_route_approve_rejects_draft_without_email(app_client):
    test_client, app = app_client
    ctx = app.extensions["cwscraper"]
    no_email = _sample_draft()
    no_email["to_email"] = ""
    d = ctx.inbound_drafts.enqueue(no_email)
    resp = test_client.post(f"/api/replies/inbound-drafts/{d['id']}/approve")
    assert resp.status_code == 400
    assert "no recipient email" in resp.get_json()["error"]
