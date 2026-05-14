"""Tests for the pre-flight readiness checker."""
from __future__ import annotations

import pytest

from cwscraper.core.niche import load_niche
from cwscraper.core.preflight import evaluate


def _clear_env(monkeypatch):
    """Wipe every env var preflight cares about so each test starts clean."""
    for var in (
        "GOOGLE_PLACES_API_KEY", "YOUTUBE_API_KEY",
        "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
        "CWSCRAPER_SECRET", "CWSCRAPER_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------- directory mode -----------------------------------------------

def test_directory_blocked_without_places_key(monkeypatch):
    _clear_env(monkeypatch)
    niche = load_niche("senior_care_agencies_se")
    pf = evaluate(niche)
    assert pf.ready is False
    assert pf.status == "blocked"
    codes = {b["code"] for b in pf.blockers}
    assert "no_google_places_key" in codes


def test_directory_ready_with_places_key(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "AIza-test-key")
    niche = load_niche("senior_care_agencies_se")
    pf = evaluate(niche)
    assert pf.ready is True
    assert pf.status in ("ready", "warning")  # may have CWSCRAPER_SECRET note
    assert not any(b["code"] == "no_google_places_key" for b in pf.blockers)


def test_env_status_reflects_what_is_set(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "x")
    monkeypatch.setenv("YOUTUBE_API_KEY", "y")
    niche = load_niche("senior_care_agencies_se")
    pf = evaluate(niche)
    assert pf.env_status["GOOGLE_PLACES_API_KEY"] is True
    assert pf.env_status["YOUTUBE_API_KEY"] is True
    assert pf.env_status["REDDIT_CLIENT_ID"] is False


def test_empty_env_value_treated_as_unset(monkeypatch):
    _clear_env(monkeypatch)
    # Empty string should not count as "set"
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "")
    monkeypatch.setenv("YOUTUBE_API_KEY", "   ")  # whitespace only
    niche = load_niche("senior_care_agencies_se")
    pf = evaluate(niche)
    assert pf.env_status["GOOGLE_PLACES_API_KEY"] is False
    assert pf.env_status["YOUTUBE_API_KEY"] is False
    assert any(b["code"] == "no_google_places_key" for b in pf.blockers)


# ---------- community mode ------------------------------------------------

def test_community_warns_about_reddit_oauth(monkeypatch):
    _clear_env(monkeypatch)
    niche = load_niche("caregiver")
    pf = evaluate(niche)
    # Caregiver pack has plenty of keywords + subs so no blockers
    assert pf.ready is True
    codes = {w["code"] for w in pf.warnings}
    assert "no_reddit_oauth" in codes


def test_community_no_warning_when_reddit_oauth_set(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "abc")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "def")
    niche = load_niche("caregiver")
    pf = evaluate(niche)
    assert not any(w["code"] == "no_reddit_oauth" for w in pf.warnings)


def test_community_youtube_key_as_note_not_warning(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "abc")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "def")
    niche = load_niche("caregiver")
    pf = evaluate(niche)
    # YouTube key missing = informational note, not a warning/blocker
    assert any(n["code"] == "no_youtube_api_key" for n in pf.notes)


def test_blank_pack_blocks_with_no_keywords(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "abc")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "def")
    niche = load_niche("blank")
    pf = evaluate(niche)
    assert pf.ready is False
    assert any(b["code"] == "no_keywords" for b in pf.blockers)


# ---------- shape ---------------------------------------------------------

def test_to_dict_includes_all_fields(monkeypatch):
    _clear_env(monkeypatch)
    niche = load_niche("senior_care_agencies_se")
    pf = evaluate(niche)
    d = pf.to_dict()
    assert "status" in d
    assert "ready" in d
    assert "blockers" in d
    assert "warnings" in d
    assert "notes" in d
    assert "env_status" in d


def test_default_secret_surfaces_as_note(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "x")
    # No CWSCRAPER_SECRET set
    niche = load_niche("senior_care_agencies_se")
    pf = evaluate(niche)
    assert any(n["code"] == "default_secret" for n in pf.notes)


def test_default_secret_silent_when_set(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "x")
    monkeypatch.setenv("CWSCRAPER_SECRET", "a-real-secret")
    niche = load_niche("senior_care_agencies_se")
    pf = evaluate(niche)
    assert not any(n["code"] == "default_secret" for n in pf.notes)
