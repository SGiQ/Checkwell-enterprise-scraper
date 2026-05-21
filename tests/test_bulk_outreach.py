"""End-to-end-ish tests for bulk_draft_and_queue().

Covers the integration of personalizer + outreach drafter + suppression +
send-limit hygiene + queue staggering.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cwscraper.core.models import BusinessLead
from cwscraper.core.niche import load_niche
from cwscraper.core.store import JSONRepository
from cwscraper.email.bulk import bulk_draft_and_queue
from cwscraper.email.queue import ScheduledEmailQueue
from cwscraper.email.suppression import SuppressionList
from cwscraper.replies.personalizer import Personalizer


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_SEND_WARMUP_DISABLED", "true")
    for var in (
        "CWSCRAPER_SEND_DAILY_CAP",
        "CWSCRAPER_SEND_PER_DOMAIN_DAILY_CAP",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> JSONRepository:
    return JSONRepository(tmp_path)


@pytest.fixture
def queue(tmp_path: Path) -> ScheduledEmailQueue:
    return ScheduledEmailQueue(tmp_path)


@pytest.fixture
def suppression(tmp_path: Path) -> SuppressionList:
    return SuppressionList(tmp_path)


@pytest.fixture
def pace_niche():
    return load_niche("pace_programs_se")


@pytest.fixture
def mock_personalizer():
    """A Personalizer wired to a mocked Anthropic client. Returns a unique
    opener per business so we can verify each one ends up in its own
    queue entry."""
    mock_client = MagicMock()

    def fake_create(*, model, max_tokens, system, messages, **_kw):
        # Echo the business name into the opener so tests can assert on it
        user_msg = messages[0]["content"]
        # Pull the business name from the user message (after "- Name: ")
        name = "Unknown"
        for line in user_msg.split("\n"):
            if line.startswith("- Name:"):
                name = line.split(":", 1)[1].strip()
                break
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="text",
                text=f"Personalized opener for {name} based on their data.",
            )],
            usage=SimpleNamespace(
                input_tokens=200, output_tokens=30,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )

    mock_client.messages.create.side_effect = fake_create
    return Personalizer(client=mock_client, api_key="test")


@pytest.fixture
def three_businesses(repo: JSONRepository):
    """Seed three business rows the bulk drafter can pull."""
    businesses = [
        BusinessLead(
            id="biz1", source="google_places", name="Heartland PACE",
            city="Greenville", state="SC", category="pace_program",
            email="info@heartlandpace.example",
        ),
        BusinessLead(
            id="biz2", source="google_places", name="Coastal PACE Center",
            city="Charleston", state="SC", category="pace_program",
            email="contact@coastalpace.example",
        ),
        BusinessLead(
            id="biz3", source="google_places", name="Mountain Pace Services",
            city="Asheville", state="NC", category="pace_program",
            email="hello@mountainpace.example",
        ),
    ]
    repo.add_businesses(businesses)
    return businesses


# ---------- happy path ----------------------------------------------------

def test_bulk_queues_all_three(repo, queue, suppression, pace_niche,
                                mock_personalizer, three_businesses):
    summary = bulk_draft_and_queue(
        business_ids=["biz1", "biz2", "biz3"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
        cadence_seconds=120,
    )
    assert summary["ok"]
    assert summary["received"] == 3
    assert summary["queued"] == 3
    assert summary["personalized"] == 3
    assert len(summary["results"]) == 3
    # Each result has a queue_id and a future scheduled_for
    for r in summary["results"]:
        assert r["status"] == "queued"
        assert r["queue_id"]
        assert r["scheduled_for"]


def test_bulk_staggers_send_times(repo, queue, suppression, pace_niche,
                                   mock_personalizer, three_businesses):
    """Consecutive sends must be at least cadence_seconds apart."""
    summary = bulk_draft_and_queue(
        business_ids=["biz1", "biz2", "biz3"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
        cadence_seconds=180,
    )
    times = [datetime.fromisoformat(r["scheduled_for"])
             for r in summary["results"]]
    # Each pair is 180 seconds apart
    assert (times[1] - times[0]).total_seconds() == 180
    assert (times[2] - times[1]).total_seconds() == 180


def test_bulk_personalizes_each_uniquely(repo, queue, suppression, pace_niche,
                                          mock_personalizer, three_businesses):
    """Each business gets its own opener — verifiable in the queued body."""
    bulk_draft_and_queue(
        business_ids=["biz1", "biz2", "biz3"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    bodies = [e["body"] for e in queue.list()]
    assert any("Heartland PACE" in b for b in bodies)
    assert any("Coastal PACE Center" in b for b in bodies)
    assert any("Mountain Pace Services" in b for b in bodies)


# ---------- skipping paths -----------------------------------------------

def test_bulk_skips_suppressed(repo, queue, suppression, pace_niche,
                                mock_personalizer, three_businesses):
    suppression.add("info@heartlandpace.example", reason="unsubscribe")
    summary = bulk_draft_and_queue(
        business_ids=["biz1", "biz2", "biz3"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    assert summary["queued"] == 2
    assert summary["skipped_suppressed"] == 1
    skipped = [r for r in summary["results"] if r["status"] == "skipped_suppressed"]
    assert skipped[0]["business_id"] == "biz1"


def test_bulk_skips_business_without_email(repo, queue, suppression, pace_niche,
                                            mock_personalizer):
    repo.add_businesses([
        BusinessLead(id="biz_no_email", source="google_places",
                     name="No Email Co", city="Tampa", state="FL", email=""),
    ])
    summary = bulk_draft_and_queue(
        business_ids=["biz_no_email"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    assert summary["queued"] == 0
    assert summary["skipped_no_email"] == 1


def test_bulk_uses_contact_email_when_business_email_missing(
    repo, queue, suppression, pace_niche, mock_personalizer
):
    repo.add_businesses([
        BusinessLead(
            id="biz_contact", source="google_places",
            name="Contact Only", city="Tampa", state="FL", email="",
            contacts=[{"name": "Jane", "email": "jane@contactonly.example"}],
        ),
    ])
    summary = bulk_draft_and_queue(
        business_ids=["biz_contact"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    assert summary["queued"] == 1
    assert summary["results"][0]["to_email"] == "jane@contactonly.example"


def test_bulk_skips_unknown_business_id(repo, queue, suppression, pace_niche,
                                         mock_personalizer, three_businesses):
    summary = bulk_draft_and_queue(
        business_ids=["biz1", "nonexistent"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    assert summary["queued"] == 1
    assert summary["skipped_not_found"] == 1


def test_bulk_stops_when_daily_cap_hit(repo, queue, suppression, pace_niche,
                                        mock_personalizer, three_businesses,
                                        monkeypatch):
    monkeypatch.setenv("CWSCRAPER_SEND_DAILY_CAP", "2")
    summary = bulk_draft_and_queue(
        business_ids=["biz1", "biz2", "biz3"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    assert summary["queued"] == 2
    # The third was rejected on the daily-cap check
    assert summary["skipped_hygiene"] >= 1


def test_bulk_rejects_empty_business_ids(repo, queue, suppression, pace_niche,
                                          mock_personalizer):
    summary = bulk_draft_and_queue(
        business_ids=[],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    assert not summary["ok"]
    assert "no business IDs" in summary["error"]


def test_bulk_rejects_oversized_batch(repo, queue, suppression, pace_niche,
                                       mock_personalizer):
    summary = bulk_draft_and_queue(
        business_ids=[f"biz{i}" for i in range(300)],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=mock_personalizer,
    )
    assert not summary["ok"]
    assert "too large" in summary["error"]


def test_bulk_works_without_personalizer(repo, queue, suppression, pace_niche,
                                          three_businesses):
    """No personalizer → opener is empty, but draft still ships. Bulk
    drafting should not fail when ANTHROPIC_API_KEY is unset."""
    summary = bulk_draft_and_queue(
        business_ids=["biz1"],
        repo=repo, queue=queue, suppression=suppression,
        niche=pace_niche, personalizer=None,
    )
    assert summary["queued"] == 1
    # The PACE template uses {personalized_opener} — without a personalizer,
    # the token resolves to empty. The body still renders.
    body = queue.list()[0]["body"]
    assert "{personalized_opener}" not in body
    assert "Heartland PACE" in body
