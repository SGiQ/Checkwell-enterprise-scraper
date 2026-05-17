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


def test_batch_enrichment_lands_in_scan_history(app_client, tmp_path, monkeypatch):
    """Completed enrichment runs should appear in /api/scan-logs alongside scans."""
    from cwscraper.enrichment import website_scraper
    from cwscraper.enrichment.base import EnrichmentResult

    # Mock the enricher to return predictable data
    def fake_enrich(self, business, ctx):
        return EnrichmentResult(email=f"found@{business.get('name', 'x').lower()}.example",
                                contacts=[], source="website")

    monkeypatch.setattr(website_scraper.WebsiteScraper, "enrich", fake_enrich)

    # Run batch enrichment via the engine (synchronously)
    from cwscraper.web.app import create_app
    app = create_app()
    ctx_obj = app.extensions["cwscraper"]
    ctx_obj.engine.run_enrichment(enricher_slug="website", only_missing_email=True)

    # Now hit /api/scan-logs and verify the enrichment run is there
    r = app_client.get("/api/scan-logs")
    logs = r.get_json()
    enrichment_logs = [l for l in logs if l.get("kind") == "enrichment"]
    assert enrichment_logs, "enrichment run did not appear in scan history"

    entry = enrichment_logs[0]
    assert entry["enricher"] == "website"
    assert entry["status"] in ("complete", "cancelled")
    assert "businesses_done" in entry
    assert "emails_found" in entry
    assert "niche" in entry
    assert "timestamp" in entry


def test_businesses_filter_by_niche(app_client, tmp_path):
    """The /api/businesses?niche=... filter should respect source_niches tags."""
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([
        BusinessLead(id="b1", name="Senior Care Co",   source_niches=["senior_care_agencies_se"]),
        BusinessLead(id="b2", name="PACE Center",      source_niches=["pace_programs_se"]),
        BusinessLead(id="b3", name="Multi-niche Co",
                     source_niches=["senior_care_agencies_se", "pace_programs_se"]),
    ])

    # All niches
    all_rows = app_client.get("/api/businesses").get_json()["businesses"]
    assert {b["id"] for b in all_rows} == {"b1", "b2", "b3", "biz1"}  # biz1 is the fixture seed

    # Filter to PACE only — should return b2 + b3 (multi-niche row matches)
    pace = app_client.get("/api/businesses?niche=pace_programs_se").get_json()["businesses"]
    assert {b["id"] for b in pace} == {"b2", "b3"}

    # Filter to senior_care_agencies_se — should return b1 + b3
    sca = app_client.get("/api/businesses?niche=senior_care_agencies_se").get_json()["businesses"]
    assert {b["id"] for b in sca} == {"b1", "b3"}


def test_businesses_niches_endpoint_counts(app_client, tmp_path):
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([
        BusinessLead(id="b1", name="A", source_niches=["pace_programs_se"]),
        BusinessLead(id="b2", name="B", source_niches=["pace_programs_se"]),
        BusinessLead(id="b3", name="C", source_niches=["senior_care_agencies_se"]),
        BusinessLead(id="b4", name="Untagged"),    # no source_niches
    ])
    d = app_client.get("/api/businesses/niches").get_json()
    by_slug = {n["slug"]: n["count"] for n in d["niches"]}
    assert by_slug["pace_programs_se"] == 2
    assert by_slug["senior_care_agencies_se"] == 1
    # 'biz1' from fixture is also untagged so untagged count is >= 1
    assert d["untagged"] >= 1


def test_backfill_niches_from_category(app_client, tmp_path):
    """Untagged rows get source_niches stamped based on their category field."""
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([
        # category matches senior_care_agencies_se's category_label
        BusinessLead(id="b1", name="Acme Home Care", category="senior_care_agency"),
        # category matches pace_programs_se
        BusinessLead(id="b2", name="Tampa PACE", category="pace_program"),
        # category matches area_agencies_on_aging_se
        BusinessLead(id="b3", name="SC Aging Council", category="area_agency_on_aging"),
        # Unknown category — should not get tagged
        BusinessLead(id="b4", name="Unknown Co", category="some_random_type"),
        # Already tagged — should be skipped
        BusinessLead(id="b5", name="Already Tagged",
                     category="senior_care_agency",
                     source_niches=["senior_care_agencies_se"]),
    ])

    r = app_client.post("/api/businesses/backfill-niches")
    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["tagged"] == 3                # b1, b2, b3
    assert body["skipped_already_tagged"] >= 1  # b5 (and fixture biz1 maybe)
    unmatched_cats = {u["category"] for u in body["unmatched"]}
    assert "some_random_type" in unmatched_cats

    # Verify the file actually got the tags
    rows = repo.get_businesses()
    by_id = {b["id"]: b for b in rows}
    assert by_id["b1"]["source_niches"] == ["senior_care_agencies_se"]
    assert by_id["b2"]["source_niches"] == ["pace_programs_se"]
    assert by_id["b3"]["source_niches"] == ["area_agencies_on_aging_se"]
    assert by_id["b4"]["source_niches"] == []
    assert by_id["b5"]["source_niches"] == ["senior_care_agencies_se"]


def test_backfill_is_idempotent(app_client, tmp_path):
    """Running backfill twice should be a no-op the second time."""
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([
        BusinessLead(id="x", name="X", category="pace_program"),
    ])
    first = app_client.post("/api/businesses/backfill-niches").get_json()
    second = app_client.post("/api/businesses/backfill-niches").get_json()
    assert first["tagged"] == 1
    assert second["tagged"] == 0
    assert second["skipped_already_tagged"] >= 1


def test_businesses_filter_has_email(app_client, tmp_path):
    repo = JSONRepository(data_dir=tmp_path)
    repo.add_businesses([
        BusinessLead(id="b1", name="With Email",    email="ok@x.example"),
        BusinessLead(id="b2", name="Without Email", email=""),
    ])
    with_email = app_client.get("/api/businesses?has_email=true").get_json()["businesses"]
    ids = {b["id"] for b in with_email}
    assert "b1" in ids
    assert "b2" not in ids


def test_discovery_scan_log_includes_kind_and_niche(app_client):
    """Discovery scans must also be tagged so the renderer can discriminate."""
    # The seed niche is senior_care_agencies_se (directory mode); we don't
    # actually want to hit Google Places here, but we can verify the tag
    # logic at the engine level using an empty repo.
    from cwscraper.web.app import create_app
    app = create_app()
    ctx_obj = app.extensions["cwscraper"]
    # Force a directory scan with no API key -> the run still produces a log
    # entry with kind=scan + mode=directory + niche=...
    ctx_obj.engine.run_full_scan()
    logs = app_client.get("/api/scan-logs").get_json()
    scan_logs = [l for l in logs if l.get("kind") == "scan"]
    assert scan_logs, "discovery scan did not appear in history"
    entry = scan_logs[0]
    assert entry["mode"] == "directory"
    assert entry["niche"] == "senior_care_agencies_se"