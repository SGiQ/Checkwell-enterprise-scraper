"""Tests for runtime niche-pack switching."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cwscraper.core.niche import list_bundled_niches, load_niche


def test_list_bundled_niches_returns_metadata():
    out = list_bundled_niches()
    assert len(out) >= 2
    slugs = {n["slug"] for n in out}
    assert "caregiver" in slugs
    assert "senior_care_agencies_se" in slugs

    caregiver = next(n for n in out if n["slug"] == "caregiver")
    assert caregiver["mode"] == "community"
    assert caregiver["display_name"]
    assert "description" in caregiver

    se = next(n for n in out if n["slug"] == "senior_care_agencies_se")
    assert se["mode"] == "directory"


def test_list_bundled_niches_includes_modes():
    out = list_bundled_niches()
    modes = {n["mode"] for n in out}
    # Should have at least one of each
    assert "community" in modes
    assert "directory" in modes


def test_all_bundled_directory_packs_load_cleanly():
    """All shipped B2B packs must load and have non-empty essentials."""
    expected_directory_packs = [
        "senior_care_agencies_se",
        "home_health_agencies_se",
        "assisted_living_facilities_se",
        "memory_care_facilities_se",
        "geriatric_care_managers_us",
    ]
    available = {n["slug"] for n in list_bundled_niches()}
    for slug in expected_directory_packs:
        assert slug in available, f"missing bundled pack: {slug}"
        pack = load_niche(slug)
        assert pack.mode == "directory"
        assert pack.directory.search_queries, f"{slug} has no search_queries"
        assert pack.directory.locations, f"{slug} has no locations"
        assert pack.outreach_templates, f"{slug} has no outreach_templates"
        # Cold-intro template must exist for the dashboard's Draft Email button
        assert any(t.key == "cold_intro" for t in pack.outreach_templates), (
            f"{slug} missing 'cold_intro' outreach template"
        )


# --- AppContext tests ---

@pytest.fixture
def fresh_ctx(tmp_path, monkeypatch):
    """Build an AppContext rooted at a temp data dir, no env-var pollution."""
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CWSCRAPER_NICHE", raising=False)
    from cwscraper.web.app import AppContext  # import after env patch
    return AppContext()


def test_boot_uses_default_niche_when_nothing_configured(fresh_ctx):
    fresh_ctx.boot()
    assert fresh_ctx.niche.slug == "caregiver"
    assert fresh_ctx.engine is not None
    assert fresh_ctx.scheduler is not None


def test_boot_respects_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_NICHE", "senior_care_agencies_se")
    from cwscraper.web.app import AppContext
    ctx = AppContext()
    ctx.boot()
    assert ctx.niche.slug == "senior_care_agencies_se"
    assert ctx.niche.mode == "directory"


def test_boot_falls_back_when_niche_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_NICHE", "doesnt_exist_anywhere")
    from cwscraper.web.app import AppContext
    ctx = AppContext()
    ctx.boot()
    assert ctx.niche.slug == "caregiver"  # graceful fallback


def test_swap_niche_persists_to_config(fresh_ctx):
    fresh_ctx.boot()
    assert fresh_ctx.niche.slug == "caregiver"

    fresh_ctx.swap_niche("senior_care_agencies_se")
    assert fresh_ctx.niche.slug == "senior_care_agencies_se"
    assert fresh_ctx.niche.mode == "directory"
    assert fresh_ctx.repo.get_config()["active_niche"] == "senior_care_agencies_se"


def test_swap_niche_persists_across_app_restarts(tmp_path, monkeypatch):
    """Active niche should survive a fresh process by reading it from config."""
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CWSCRAPER_NICHE", raising=False)
    from cwscraper.web.app import AppContext

    ctx1 = AppContext()
    ctx1.boot()
    ctx1.swap_niche("senior_care_agencies_se")
    assert ctx1.niche.slug == "senior_care_agencies_se"

    # Simulate restart — new AppContext on the same data dir
    ctx2 = AppContext()
    ctx2.boot()
    assert ctx2.niche.slug == "senior_care_agencies_se"


def test_swap_niche_blocked_during_scan(fresh_ctx):
    fresh_ctx.boot()
    fresh_ctx.engine.is_scanning = True
    with pytest.raises(RuntimeError, match="scan or enrichment"):
        fresh_ctx.swap_niche("senior_care_agencies_se")
    # Original niche unchanged
    assert fresh_ctx.niche.slug == "caregiver"


def test_swap_niche_blocked_during_enrichment(fresh_ctx):
    fresh_ctx.boot()
    fresh_ctx.engine.is_enriching = True
    with pytest.raises(RuntimeError, match="scan or enrichment"):
        fresh_ctx.swap_niche("senior_care_agencies_se")


def test_swap_niche_replaces_engine(fresh_ctx):
    fresh_ctx.boot()
    old_engine = fresh_ctx.engine
    fresh_ctx.swap_niche("senior_care_agencies_se")
    # A fresh engine is constructed so it picks up the new niche's mode
    assert fresh_ctx.engine is not old_engine
    assert fresh_ctx.engine.niche.slug == "senior_care_agencies_se"


def test_swap_niche_unknown_slug_raises(fresh_ctx):
    fresh_ctx.boot()
    with pytest.raises(FileNotFoundError):
        fresh_ctx.swap_niche("does_not_exist")
