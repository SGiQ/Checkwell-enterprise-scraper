"""Tests for manual contact editing + single-business re-enrichment."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cwscraper.core.models import BusinessLead
from cwscraper.core.store import JSONRepository


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CWSCRAPER_NICHE", raising=False)
    monkeypatch.setenv("CWSCRAPER_NICHE", "senior_care_agencies_se")
    # Seed one business
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([BusinessLead(
        id="biz1", name="Acme Senior Care", city="Tampa", state="FL",
        website="https://acme.example",
    )])

    from cwscraper.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---------- POST /api/businesses/<id>/contact validation ----------------

def test_set_valid_email(app_client):
    r = app_client.post("/api/businesses/biz1/contact", json={"email": "owner@acme.example"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["patch"]["email"] == "owner@acme.example"


def test_set_invalid_email_rejected(app_client):
    r = app_client.post("/api/businesses/biz1/contact", json={"email": "not-an-email"})
    assert r.status_code == 400
    assert "Invalid email" in r.get_json()["error"]


def test_clear_email_with_empty_string(app_client):
    # First set, then clear
    app_client.post("/api/businesses/biz1/contact", json={"email": "x@acme.example"})
    r = app_client.post("/api/businesses/biz1/contact", json={"email": ""})
    assert r.status_code == 200
    # Verify via list endpoint
    biz_list = app_client.get("/api/businesses").get_json()["businesses"]
    assert next(b for b in biz_list if b["id"] == "biz1")["email"] == ""


def test_email_is_lowercased(app_client):
    r = app_client.post("/api/businesses/biz1/contact", json={"email": "OWNER@ACME.EXAMPLE"})
    assert r.get_json()["patch"]["email"] == "owner@acme.example"


def test_contacts_array_with_name_and_title(app_client):
    r = app_client.post("/api/businesses/biz1/contact", json={
        "contacts": [{"name": "Janet Smith", "title": "Director", "email": "janet@acme.example"}]
    })
    assert r.status_code == 200
    contacts = r.get_json()["patch"]["contacts"]
    assert contacts[0]["name"] == "Janet Smith"
    assert contacts[0]["title"] == "Director"
    assert contacts[0]["email"] == "janet@acme.example"
    # source_url defaults to 'manual' when not provided
    assert contacts[0]["source_url"] == "manual"


def test_contacts_with_invalid_email_rejected(app_client):
    r = app_client.post("/api/businesses/biz1/contact", json={
        "contacts": [{"name": "X", "email": "garbage"}]
    })
    assert r.status_code == 400


def test_empty_body_rejected(app_client):
    r = app_client.post("/api/businesses/biz1/contact", json={})
    assert r.status_code == 400


# ---------- POST /api/businesses/<id>/enrich (single re-enrich) ---------

def test_enrich_one_requires_existing_business(app_client):
    r = app_client.post("/api/businesses/nope/enrich", json={"enricher": "website"})
    assert r.status_code == 404


def test_enrich_one_rejects_unknown_enricher(app_client):
    r = app_client.post("/api/businesses/biz1/enrich", json={"enricher": "magic"})
    assert r.status_code == 400
    assert "Unknown enricher" in r.get_json()["error"]


def test_enrich_one_requires_website(app_client, tmp_path):
    # Add a business with no website
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([BusinessLead(id="biz_nosite", name="No Site Co")])
    r = app_client.post("/api/businesses/biz_nosite/enrich", json={"enricher": "website"})
    assert r.status_code == 422


def test_enrich_one_clears_email_first(app_client, tmp_path, monkeypatch):
    """clear_first=True wipes the existing email before running."""
    # Pre-seed an email
    app_client.post("/api/businesses/biz1/contact", json={"email": "old@acme.example"})

    # Mock the website scraper to return a new email
    from cwscraper.enrichment import website_scraper
    from cwscraper.enrichment.base import EnrichmentResult

    def fake_enrich(self, business, ctx):
        # confirm the email was cleared before this enricher saw the row
        assert business.get("email") == "", f"expected cleared email, got: {business.get('email')!r}"
        return EnrichmentResult(email="new@acme.example", contacts=[], source="website")

    monkeypatch.setattr(website_scraper.WebsiteScraper, "enrich", fake_enrich)

    r = app_client.post("/api/businesses/biz1/enrich", json={"enricher": "website", "clear_first": True})
    body = r.get_json()
    assert r.status_code == 200
    assert body["email"] == "new@acme.example"