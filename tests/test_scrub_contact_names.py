"""Tests for POST /api/businesses/scrub-contact-names.

Clears stale junk contact names that fail the current _looks_like_name()
filter — used after deploying contact-name filter changes to retrofit
the JSON store.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cwscraper.core.models import BusinessLead


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_NICHE", "caregiver")
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
                "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD",
                "RESEND_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from cwscraper.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), app


def _seed_mixed_businesses(app):
    """Seed three businesses: one clean, one with junk, one with mixed
    contacts (one clean, one junk)."""
    ctx = app.extensions["cwscraper"]
    ctx.repo.add_businesses([
        BusinessLead(
            id="biz_clean", source="google_places",
            name="Visiting Angels Tampa", city="Tampa", state="FL",
            email="contact@vatampa.example",
            contacts=[{"name": "Janet Smith", "email": "janet@vatampa.example"}],
        ),
        BusinessLead(
            id="biz_junk", source="google_places",
            name="Professional Home Health Services", city="Miami", state="FL",
            email="info@phhs.example",
            contacts=[{
                "name": "Accessibility Tools Accessibility",
                "email": "info@phhs.example",
            }],
        ),
        BusinessLead(
            id="biz_mixed", source="google_places",
            name="Mixed Co", city="Orlando", state="FL",
            email="hello@mixed.example",
            contacts=[
                {"name": "Cookie Settings", "email": "c1@mixed.example"},
                {"name": "Maria Rodriguez", "email": "c2@mixed.example"},
            ],
        ),
    ])
    return ctx


def test_scrub_dry_run_counts_without_modifying(client):
    test_client, app = client
    _seed_mixed_businesses(app)

    resp = test_client.post(
        "/api/businesses/scrub-contact-names",
        json={"dry_run": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["dry_run"] is True
    assert body["businesses_scanned"] == 3
    assert body["contacts_scanned"] == 4  # 1 + 1 + 2
    assert body["names_cleared"] == 2     # 'Accessibility...' + 'Cookie Settings'
    assert body["businesses_affected"] == 2

    # Verify nothing was actually written
    ctx = app.extensions["cwscraper"]
    biz_junk = next(b for b in ctx.repo.get_businesses() if b["id"] == "biz_junk")
    assert biz_junk["contacts"][0]["name"] == "Accessibility Tools Accessibility"


def test_scrub_apply_clears_junk_keeps_emails(client):
    test_client, app = client
    _seed_mixed_businesses(app)

    resp = test_client.post(
        "/api/businesses/scrub-contact-names",
        json={"dry_run": False},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["names_cleared"] == 2
    assert body["businesses_affected"] == 2

    ctx = app.extensions["cwscraper"]
    businesses = {b["id"]: b for b in ctx.repo.get_businesses()}

    # Clean business is untouched
    assert businesses["biz_clean"]["contacts"][0]["name"] == "Janet Smith"

    # Junk business has the name cleared but email + contact list preserved
    junk = businesses["biz_junk"]
    assert junk["contacts"][0]["name"] == ""
    assert junk["contacts"][0]["email"] == "info@phhs.example"
    assert junk["email"] == "info@phhs.example"

    # Mixed business: junk cleared, clean preserved
    mixed = businesses["biz_mixed"]
    assert mixed["contacts"][0]["name"] == ""           # Cookie Settings → ""
    assert mixed["contacts"][0]["email"] == "c1@mixed.example"
    assert mixed["contacts"][1]["name"] == "Maria Rodriguez"
    assert mixed["contacts"][1]["email"] == "c2@mixed.example"


def test_scrub_idempotent(client):
    """Running the scrub twice in a row finds nothing on the second pass."""
    test_client, app = client
    _seed_mixed_businesses(app)

    test_client.post("/api/businesses/scrub-contact-names", json={"dry_run": False})

    resp = test_client.post(
        "/api/businesses/scrub-contact-names",
        json={"dry_run": False},
    )
    body = resp.get_json()
    assert body["names_cleared"] == 0
    assert body["businesses_affected"] == 0


def test_scrub_sample_capped_at_25(client):
    """The sample list never exceeds 25 entries even if affected count is higher."""
    test_client, app = client
    ctx = app.extensions["cwscraper"]
    # Seed 40 junk businesses
    ctx.repo.add_businesses([
        BusinessLead(
            id=f"biz_{i}", source="google_places",
            name=f"Junk Co {i}", city="X", state="FL",
            email=f"c{i}@x.example",
            contacts=[{"name": "Read More Now", "email": f"c{i}@x.example"}],
        )
        for i in range(40)
    ])

    resp = test_client.post(
        "/api/businesses/scrub-contact-names",
        json={"dry_run": True},
    )
    body = resp.get_json()
    assert body["names_cleared"] == 40
    assert len(body["sample"]) == 25


def test_scrub_handles_empty_repo(client):
    test_client, _ = client
    resp = test_client.post("/api/businesses/scrub-contact-names", json={})
    body = resp.get_json()
    assert body["ok"] is True
    assert body["businesses_scanned"] == 0
    assert body["names_cleared"] == 0


def test_scrub_defaults_to_apply_mode_not_dry_run(client):
    """Omitting dry_run from the body should ACTUALLY apply the change.
    Caller can opt into dry_run; default is "do it."""
    test_client, app = client
    _seed_mixed_businesses(app)

    resp = test_client.post("/api/businesses/scrub-contact-names", json={})
    body = resp.get_json()
    assert body["dry_run"] is False
    assert body["names_cleared"] == 2

    ctx = app.extensions["cwscraper"]
    biz_junk = next(b for b in ctx.repo.get_businesses() if b["id"] == "biz_junk")
    assert biz_junk["contacts"][0]["name"] == ""
