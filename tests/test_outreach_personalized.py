"""Tests for draft_outreach()'s integration with the Personalizer.

Verifies that {personalized_opener} is only filled when the template uses
the token AND a personalizer is provided. Templates without the token
keep working unchanged (regression coverage).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cwscraper.core.niche import load_niche
from cwscraper.replies.outreach import draft_outreach
from cwscraper.replies.personalizer import Personalizer


@pytest.fixture
def pace_niche():
    return load_niche("pace_programs_se")


@pytest.fixture
def business():
    return {
        "id": "biz1",
        "name": "Heartland PACE",
        "city": "Greenville", "state": "SC",
        "category": "pace_program",
        "rating": 4.7, "review_count": 56,
        "email": "info@heartlandpace.example",
    }


def _mock_personalizer(opener: str = "Test opener sentence."):
    """Build a Personalizer with a mocked Anthropic client."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=opener)],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    return Personalizer(client=mock_client, api_key="test")


def test_draft_outreach_no_personalizer_uses_empty_opener(pace_niche, business):
    """Template that uses {personalized_opener} but no personalizer passed:
    token resolves to empty string, draft still renders."""
    draft = draft_outreach(business, pace_niche, template_key="referral_partnership")
    assert "{personalized_opener}" not in draft["body"]
    assert draft["personalized_opener"] == ""
    assert "Heartland PACE" in draft["body"]


def test_draft_outreach_with_personalizer_fills_opener(pace_niche, business):
    p = _mock_personalizer("This is a personalized opener for Heartland PACE.")
    draft = draft_outreach(
        business, pace_niche,
        template_key="referral_partnership",
        personalizer=p,
    )
    assert "personalized opener for Heartland PACE" in draft["body"]
    assert draft["personalized_opener"] == \
        "This is a personalized opener for Heartland PACE."
    assert draft["personalization"] is not None
    assert draft["personalization"]["error"] is None


def test_draft_outreach_skips_personalizer_when_template_unused(pace_niche, business):
    """Follow-up templates intentionally don't use {personalized_opener} —
    they're meant to be terse refreshers, not new personalized intros.
    Even with a personalizer passed, no API call should happen for these
    templates — saves cost and avoids opener fatigue in the conversation."""
    p = _mock_personalizer("Should not be used.")
    p.client.messages.create.reset_mock()  # fresh count

    draft = draft_outreach(
        business, pace_niche,
        template_key="follow_up",
        personalizer=p,
    )
    assert draft["personalized_opener"] == ""
    assert draft["personalization"] is None
    # Critical: the API was not called for a template that doesn't use it
    p.client.messages.create.assert_not_called()


def test_draft_outreach_unknown_template_returns_empty(pace_niche, business):
    """Defensive: a bad template_key shouldn't blow up."""
    p = _mock_personalizer()
    draft = draft_outreach(
        business, pace_niche,
        template_key="nonexistent-template",
        personalizer=p,
    )
    # Falls through to first available template (defensive)
    assert draft["subject"]  # something rendered
    assert draft["template_used"]


def test_draft_outreach_personalizer_fallback_on_api_failure(pace_niche, business):
    """If the personalizer raises, the draft still ships (with the
    regional fallback opener). Bulk drafting must never crash on a
    single AI failure."""
    p = _mock_personalizer()
    p.client.messages.create.side_effect = RuntimeError("API down")

    draft = draft_outreach(
        business, pace_niche,
        template_key="referral_partnership",
        personalizer=p,
    )
    # Body still rendered; opener is the fallback
    assert "Heartland PACE" in draft["body"]
    assert "Heartland PACE" in draft["personalized_opener"]  # fallback uses name
    assert draft["personalization"]["error"] is not None
