"""Tests for the send-volume hygiene module.

Daily cap, per-domain cap, and warm-up curve. Uses tmp_path so each test
gets an isolated data directory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cwscraper.email.queue import ScheduledEmailQueue
from cwscraper.email.send_limits import (
    _warmup_day,
    check_can_queue,
    effective_daily_cap,
    record_first_send_date_if_unset,
    settings_summary,
    todays_counts,
)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    # Clear send-limit env vars so each test sees defaults
    for var in (
        "CWSCRAPER_SEND_DAILY_CAP",
        "CWSCRAPER_SEND_PER_DOMAIN_DAILY_CAP",
        "CWSCRAPER_SEND_WARMUP_DISABLED",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def queue(tmp_path: Path) -> ScheduledEmailQueue:
    return ScheduledEmailQueue(tmp_path)


def _today_at(hour: int = 9) -> str:
    return datetime.now(timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    ).isoformat()


# ---------- Effective daily cap (warm-up curve) --------------------------

def test_effective_cap_equals_base_when_never_sent():
    """No first_send_date file → no warm-up applied → base cap returned."""
    assert effective_daily_cap(base_cap=50) == 50


def test_effective_cap_first_week_is_10(tmp_path: Path):
    """Warm-up curve: day 0–6 caps at 10 (or base, whichever is lower)."""
    record_first_send_date_if_unset()  # stamps today
    assert effective_daily_cap(base_cap=50) == 10


def test_effective_cap_disabled_via_env(monkeypatch):
    record_first_send_date_if_unset()
    monkeypatch.setenv("CWSCRAPER_SEND_WARMUP_DISABLED", "true")
    assert effective_daily_cap(base_cap=50) == 50


def test_warmup_day_returns_zero_without_first_send():
    assert _warmup_day() == 0


def test_record_first_send_is_idempotent(tmp_path: Path):
    record_first_send_date_if_unset()
    first_value = (tmp_path / "first_send_date.txt").read_text()
    # Wait, write again, value unchanged
    record_first_send_date_if_unset()
    second_value = (tmp_path / "first_send_date.txt").read_text()
    assert first_value == second_value


# ---------- todays_counts ------------------------------------------------

def test_todays_counts_empty_when_no_queue_entries(queue):
    assert todays_counts(queue) == {"total": 0, "by_domain": {}}


def test_todays_counts_buckets_by_domain(queue):
    queue.enqueue(prospect_id="b1", lead_type="business",
                  to_email="a@example.com", subject="s", body="b",
                  scheduled_for=_today_at(9))
    queue.enqueue(prospect_id="b2", lead_type="business",
                  to_email="b@example.com", subject="s", body="b",
                  scheduled_for=_today_at(10))
    queue.enqueue(prospect_id="b3", lead_type="business",
                  to_email="c@other.com", subject="s", body="b",
                  scheduled_for=_today_at(11))
    counts = todays_counts(queue)
    assert counts["total"] == 3
    assert counts["by_domain"] == {"example.com": 2, "other.com": 1}


def test_todays_counts_excludes_other_days(queue):
    """Yesterday's queue entries shouldn't count toward today's cap."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    queue.enqueue(prospect_id="b1", lead_type="business",
                  to_email="a@example.com", subject="s", body="b",
                  scheduled_for=yesterday)
    queue.enqueue(prospect_id="b2", lead_type="business",
                  to_email="b@example.com", subject="s", body="b",
                  scheduled_for=_today_at(9))
    counts = todays_counts(queue)
    assert counts["total"] == 1


# ---------- check_can_queue ----------------------------------------------

def test_check_rejects_missing_email(queue):
    allowed, reason = check_can_queue("", queue)
    assert not allowed
    assert "missing-or-invalid-email" in reason


def test_check_rejects_invalid_email(queue):
    allowed, reason = check_can_queue("not-an-email", queue)
    assert not allowed


def test_check_allows_under_caps(queue):
    allowed, reason = check_can_queue("new@example.com", queue)
    assert allowed
    assert reason == ""


def test_check_blocks_when_daily_cap_hit(queue, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_SEND_DAILY_CAP", "3")
    monkeypatch.setenv("CWSCRAPER_SEND_WARMUP_DISABLED", "true")
    for i in range(3):
        queue.enqueue(prospect_id=f"b{i}", lead_type="business",
                      to_email=f"u{i}@diff{i}.com",  # different domains
                      subject="s", body="b", scheduled_for=_today_at(9))
    allowed, reason = check_can_queue("new@x.com", queue)
    assert not allowed
    assert "daily-cap" in reason


def test_check_blocks_when_per_domain_cap_hit(queue, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_SEND_DAILY_CAP", "100")
    monkeypatch.setenv("CWSCRAPER_SEND_PER_DOMAIN_DAILY_CAP", "2")
    monkeypatch.setenv("CWSCRAPER_SEND_WARMUP_DISABLED", "true")
    for i in range(2):
        queue.enqueue(prospect_id=f"b{i}", lead_type="business",
                      to_email=f"u{i}@example.com",
                      subject="s", body="b", scheduled_for=_today_at(9))
    allowed, reason = check_can_queue("third@example.com", queue)
    assert not allowed
    assert "per-domain-cap" in reason
    assert "example.com" in reason


def test_check_per_domain_doesnt_block_other_domains(queue, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_SEND_DAILY_CAP", "100")
    monkeypatch.setenv("CWSCRAPER_SEND_PER_DOMAIN_DAILY_CAP", "2")
    monkeypatch.setenv("CWSCRAPER_SEND_WARMUP_DISABLED", "true")
    for i in range(2):
        queue.enqueue(prospect_id=f"b{i}", lead_type="business",
                      to_email=f"u{i}@example.com",
                      subject="s", body="b", scheduled_for=_today_at(9))
    allowed, _ = check_can_queue("new@different.com", queue)
    assert allowed


# ---------- settings_summary --------------------------------------------

def test_settings_summary_exposes_warmup_state(monkeypatch):
    monkeypatch.setenv("CWSCRAPER_SEND_DAILY_CAP", "50")
    monkeypatch.setenv("CWSCRAPER_SEND_PER_DOMAIN_DAILY_CAP", "5")
    s = settings_summary()
    assert s["daily_cap_base"] == 50
    assert s["per_domain_daily_cap"] == 5
    assert s["daily_cap_effective"] == 50  # no warm-up yet
    assert s["warmup_disabled"] is False
    assert s["first_send_date"] == ""
