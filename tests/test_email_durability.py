"""Phase 1: durable queue + at-most-once claim + transient-failure retry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from cwscraper.core.store import JSONRepository
from cwscraper.email.dispatcher import EmailDispatcher
from cwscraper.email.queue import ScheduledEmailQueue
from cwscraper.email.transport import TransportError


def _past(minutes: int = 5) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def queue(tmp_path: Path) -> ScheduledEmailQueue:
    return ScheduledEmailQueue(tmp_path)


# ---------- claim / at-most-once ----------------------------------------

def test_claim_due_flips_to_sending_and_is_at_most_once(queue):
    e = queue.enqueue(prospect_id="p", lead_type="business", to_email="a@b.com",
                      subject="s", body="b", scheduled_for=_past())
    claimed = queue.claim_due()
    assert [c["id"] for c in claimed] == [e["id"]]
    assert queue.get(e["id"])["status"] == "sending"
    # Already claimed → a second claim (or a crash-restart tick) gets nothing.
    assert queue.claim_due() == []
    assert queue.due_pending() == []  # no longer 'pending'


def test_reschedule_requeues_and_bumps_attempts(queue):
    e = queue.enqueue(prospect_id="p", lead_type="business", to_email="a@b.com",
                      subject="s", body="b", scheduled_for=_past())
    queue.claim_due()
    when = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    queue.reschedule(e["id"], when, error="transient")
    row = queue.get(e["id"])
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["scheduled_for"] == when
    assert row["claimed_at"] == ""


# ---------- crash recovery ----------------------------------------------

def test_recover_stale_fails_old_sending_keeps_fresh(queue):
    old = queue.enqueue(prospect_id="p", lead_type="business", to_email="a@b.com",
                        subject="s", body="b", scheduled_for=_past())
    fresh = queue.enqueue(prospect_id="p", lead_type="business", to_email="c@d.com",
                          subject="s", body="b", scheduled_for=_past())
    # Simulate two in-flight claims: one stale (crashed), one just started.
    queue._patch(old["id"], {"status": "sending", "claimed_at": _past(60)})
    queue._patch(fresh["id"], {"status": "sending",
                               "claimed_at": datetime.now(timezone.utc).isoformat()})

    n = queue.recover_stale(lease_seconds=300)
    assert n == 1
    assert queue.get(old["id"])["status"] == "failed"
    assert "not auto-resent" in queue.get(old["id"])["error"]
    assert queue.get(fresh["id"])["status"] == "sending"  # untouched


# ---------- durability: atomic write + backup recovery -------------------

def test_all_falls_back_to_backup_when_main_corrupt(queue, tmp_path):
    queue.enqueue(prospect_id="p", lead_type="business", to_email="a@b.com",
                  subject="s", body="b", scheduled_for=_past())  # write 1 (no bak yet)
    queue.enqueue(prospect_id="p", lead_type="business", to_email="c@d.com",
                  subject="s", body="b", scheduled_for=_past())  # write 2 -> bak holds 1 row
    # Corrupt the main file; the reader must recover the last good backup.
    queue.file.write_text("{ this is not json", encoding="utf-8")
    rows = queue._all()
    assert isinstance(rows, list) and len(rows) == 1  # recovered from .bak


# ---------- dispatcher retry/backoff -------------------------------------

@pytest.fixture
def dispatcher(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("CWSCRAPER_FROM_EMAIL", "x@y.com")
    q = ScheduledEmailQueue(tmp_path)
    return EmailDispatcher(q, JSONRepository(data_dir=tmp_path))


class _RaisingTransport:
    name = "fake"
    def __init__(self, exc: Exception):
        self.exc = exc
    def send(self, **kwargs):
        raise self.exc


def _enqueue_due(q):
    return q.enqueue(prospect_id="crm:1", lead_type="external", to_email="a@b.com",
                     subject="s", body="b", scheduled_for=_past())


def test_dispatcher_retries_transient_failure(dispatcher):
    e = _enqueue_due(dispatcher.queue)
    with patch("cwscraper.email.dispatcher.get_transport",
               return_value=_RaisingTransport(TransportError("Connection timed out"))):
        dispatcher.tick()
    row = dispatcher.queue.get(e["id"])
    assert row["status"] == "pending"   # requeued, not failed
    assert row["attempts"] == 1


def test_dispatcher_fails_permanent_error(dispatcher):
    e = _enqueue_due(dispatcher.queue)
    with patch("cwscraper.email.dispatcher.get_transport",
               return_value=_RaisingTransport(TransportError("SMTP auth failed for user"))):
        dispatcher.tick()
    assert dispatcher.queue.get(e["id"])["status"] == "failed"


def test_dispatcher_gives_up_after_max_transient_attempts(dispatcher):
    e = _enqueue_due(dispatcher.queue)
    # Pre-set attempts to the cap so the next transient failure is terminal.
    from cwscraper.email.dispatcher import MAX_SEND_ATTEMPTS
    dispatcher.queue._patch(e["id"], {"attempts": MAX_SEND_ATTEMPTS})
    with patch("cwscraper.email.dispatcher.get_transport",
               return_value=_RaisingTransport(TransportError("connection reset"))):
        dispatcher.tick()
    assert dispatcher.queue.get(e["id"])["status"] == "failed"
