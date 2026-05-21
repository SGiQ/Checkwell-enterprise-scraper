"""Tests for the AI personalizer module.

The Anthropic SDK is mocked — no real API calls. We verify:
  - System prompt construction puts the cache_control on the right block
  - User message includes only the fields that are populated
  - Response parsing handles the block-list shape
  - Local cache (JSON file) round-trips
  - configured/unconfigured fallback paths
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cwscraper.core.niche import load_niche
from cwscraper.replies.personalizer import (
    DEFAULT_MODEL,
    Personalizer,
    _system_prompt_blocks,
    _user_message,
    _fallback_opener,
)


@pytest.fixture
def pace_niche():
    return load_niche("pace_programs_se")


def _mock_response(text: str, *, cache_read: int = 0, cache_write: int = 0,
                   input_tokens: int = 100, output_tokens: int = 40) -> SimpleNamespace:
    """Build a SimpleNamespace shaped like an anthropic Message response."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


# ---------- System prompt construction -----------------------------------

def test_system_prompt_has_two_blocks_cache_control_on_last(pace_niche):
    blocks = _system_prompt_blocks(pace_niche)
    assert len(blocks) == 2
    assert blocks[0].get("cache_control") is None  # stable across niches, no marker
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    # Niche name should be in the second block
    assert pace_niche.display_name in blocks[1]["text"]


def test_system_prompt_brand_voice_is_substantial(pace_niche):
    """The cached prefix must be long enough to clear Haiku 4.5's 4096-token
    minimum cacheable prefix — verify it's at least ~3000 chars (rough proxy)."""
    blocks = _system_prompt_blocks(pace_niche)
    combined = blocks[0]["text"] + blocks[1]["text"]
    assert len(combined) > 3000, f"system prompt too short for caching: {len(combined)} chars"


# ---------- User message construction ------------------------------------

def test_user_message_includes_only_populated_fields():
    biz = {
        "id": "biz1",
        "name": "Sunshine Home Care",
        "city": "Sarasota", "state": "FL",
        "rating": 4.9, "review_count": 243,
    }
    msg = _user_message(biz)
    assert "Sunshine Home Care" in msg
    assert "Sarasota, FL" in msg
    assert "4.9 (243 reviews)" in msg
    assert "Category" not in msg  # not provided → not in prompt


def test_user_message_skips_low_review_count():
    """A 5★ rating with 2 reviews is misleading; we drop low-count ratings."""
    biz = {"id": "biz1", "name": "X", "rating": 5.0, "review_count": 2}
    msg = _user_message(biz)
    assert "5.0" not in msg
    assert "2 reviews" not in msg


def test_user_message_truncates_website_excerpt():
    biz = {"id": "biz1", "name": "X", "website_excerpt": "a" * 500}
    msg = _user_message(biz)
    assert "..." in msg
    assert len(msg) < 500  # truncated, not full 500 chars


def test_user_message_converts_snake_case_category():
    biz = {"id": "biz1", "name": "X", "category": "home_health_agency"}
    msg = _user_message(biz)
    assert "home health agency" in msg


# ---------- Personalizer with mocked client ------------------------------

def test_personalize_returns_opener_on_success(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        "Your team's 4.9 rating across 243 reviews is rare consistency in Sarasota's market.",
        cache_write=4500,
    )
    p = Personalizer(client=mock_client, api_key="test-key",
                     cache_file=tmp_path / "p.json")

    result = p.personalize(
        {"id": "biz1", "name": "Sunshine", "city": "Sarasota", "state": "FL",
         "rating": 4.9, "review_count": 243},
        pace_niche,
    )

    assert result.ok()
    assert "Sarasota" in result.opener
    assert result.cache_write_tokens == 4500
    assert not result.cached_locally  # first call, no local cache hit


def test_personalize_uses_local_cache_on_repeat(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response("First-call opener.")
    p = Personalizer(client=mock_client, api_key="test-key",
                     cache_file=tmp_path / "p.json")

    biz = {"id": "biz1", "name": "X", "city": "Tucson", "state": "AZ"}
    r1 = p.personalize(biz, pace_niche)
    r2 = p.personalize(biz, pace_niche)

    assert r1.opener == r2.opener
    assert not r1.cached_locally
    assert r2.cached_locally
    # API called exactly once
    assert mock_client.messages.create.call_count == 1


def test_personalize_persists_cache_to_disk(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response("Persisted opener.")
    cache_file = tmp_path / "p.json"

    p = Personalizer(client=mock_client, api_key="test-key", cache_file=cache_file)
    p.personalize({"id": "biz1", "name": "X"}, pace_niche)

    # New Personalizer instance reads from the same file
    p2 = Personalizer(client=mock_client, api_key="test-key", cache_file=cache_file)
    r = p2.personalize({"id": "biz1", "name": "X"}, pace_niche)
    assert r.cached_locally
    assert r.opener == "Persisted opener."


def test_personalize_strips_quote_marks_and_preambles(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        '"Here\'s your opener: This is the actual opener sentence."'
    )
    p = Personalizer(client=mock_client, api_key="test-key",
                     cache_file=tmp_path / "p.json")
    result = p.personalize({"id": "biz1", "name": "X"}, pace_niche)
    assert "Here's your opener:" not in result.opener
    assert not result.opener.startswith('"')


def test_personalize_falls_back_on_api_error(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("API down")
    p = Personalizer(client=mock_client, api_key="test-key",
                     cache_file=tmp_path / "p.json")

    result = p.personalize(
        {"id": "biz1", "name": "Sunshine", "city": "Sarasota", "state": "FL"},
        pace_niche,
    )
    assert result.error is not None
    assert "API down" in result.error
    # Fallback opener should reference the business
    assert "Sunshine" in result.opener
    assert "Sarasota" in result.opener


def test_personalize_unconfigured_uses_fallback(tmp_path: Path, monkeypatch, pace_niche):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No client passed — and anthropic may or may not be importable, but
    # without an API key configured returns False
    p = Personalizer(cache_file=tmp_path / "p.json")
    # Force the configured property to false by clearing api_key on client too
    if p.client is not None:
        p.client.api_key = None

    result = p.personalize(
        {"id": "biz1", "name": "Magnolia Gardens", "city": "Macon", "state": "GA"},
        pace_niche,
    )
    assert "Magnolia Gardens" in result.opener
    assert result.error and "not configured" in result.error


def test_personalize_batch_processes_all_in_order(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _mock_response(f"Opener for biz{i}.") for i in range(1, 4)
    ]
    p = Personalizer(client=mock_client, api_key="test-key",
                     cache_file=tmp_path / "p.json")

    businesses = [{"id": f"biz{i}", "name": f"Biz {i}"} for i in range(1, 4)]
    results = p.personalize_batch(businesses, pace_niche)
    assert len(results) == 3
    assert "biz1" in results[0].opener
    assert "biz3" in results[2].opener


def test_personalize_batch_progress_callback(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response("OK")
    p = Personalizer(client=mock_client, api_key="test-key",
                     cache_file=tmp_path / "p.json")

    seen = []
    p.personalize_batch(
        [{"id": f"b{i}", "name": "X"} for i in range(3)],
        pace_niche,
        progress_cb=lambda i, n, r: seen.append((i, n)),
    )
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_forget_drops_from_cache(tmp_path: Path, pace_niche):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response("First opener.")
    p = Personalizer(client=mock_client, api_key="test-key",
                     cache_file=tmp_path / "p.json")

    p.personalize({"id": "biz1", "name": "X"}, pace_niche)
    assert p.forget("biz1") is True
    assert p.forget("biz1") is False  # already gone


# ---------- Fallback opener ----------------------------------------------

def test_fallback_opener_with_city_and_state():
    biz = {"name": "Acme Care", "city": "Tucson", "state": "AZ"}
    opener = _fallback_opener(biz)
    assert "Acme Care" in opener
    assert "Tucson" in opener
    assert "AZ" in opener


def test_fallback_opener_without_location():
    biz = {"name": "Acme Care"}
    opener = _fallback_opener(biz)
    assert "Acme Care" in opener
