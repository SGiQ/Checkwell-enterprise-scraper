"""Tests for B2B directory mode: niche pack, store, scanner parser, outreach drafter."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cwscraper.core.models import BusinessLead
from cwscraper.core.niche import load_niche
from cwscraper.core.store import JSONRepository
from cwscraper.replies.outreach import draft_outreach
from cwscraper.scanners.google_places import _parse_place


@pytest.fixture
def tmp_repo(tmp_path: Path) -> JSONRepository:
    return JSONRepository(data_dir=tmp_path)


def test_se_niche_pack_loads():
    pack = load_niche("senior_care_agencies_se")
    assert pack.mode == "directory"
    assert pack.directory.search_queries
    assert pack.directory.locations
    # SE US — should cover at least these states
    locs = " ".join(pack.directory.locations)
    for state in ["FL", "GA", "SC", "NC", "TN"]:
        assert state in locs
    assert pack.outreach_templates
    assert pack.outreach_template(pack.default_outreach_template) is not None


def test_business_store_roundtrip(tmp_repo):
    bizzes = [
        BusinessLead(id="p1", name="Acme Senior Care", city="Tampa", state="FL", rating=4.5),
        BusinessLead(id="p2", name="Bay Home Care",    city="Tampa", state="FL", rating=4.2),
    ]
    tmp_repo.add_businesses(bizzes)
    rows = tmp_repo.get_businesses()
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"p1", "p2"}

    # Re-adding the same IDs merges, doesn't duplicate
    tmp_repo.add_businesses([BusinessLead(id="p1", name="Acme Senior Care", rating=4.6)])
    rows = tmp_repo.get_businesses()
    assert len(rows) == 2
    p1 = next(r for r in rows if r["id"] == "p1")
    assert p1["rating"] == 4.6


def test_business_status_updates(tmp_repo):
    tmp_repo.add_businesses([BusinessLead(id="x", name="X", status="new")])
    tmp_repo.update_business_status("x", "qualified")
    assert tmp_repo.get_businesses()[0]["status"] == "qualified"


def test_business_contact_enrichment_preserved_on_rescan(tmp_repo):
    tmp_repo.add_businesses([BusinessLead(id="x", name="X")])
    tmp_repo.update_business("x", {"email": "owner@x.com", "contacts": [{"name": "Jane"}]})
    # Re-add via "scan" — discovery fields refresh, but enrichment fields stick
    tmp_repo.add_businesses([BusinessLead(id="x", name="X Renamed", rating=4.8)])
    row = tmp_repo.get_businesses()[0]
    assert row["name"] == "X Renamed"
    assert row["rating"] == 4.8
    assert row["email"] == "owner@x.com"
    assert row["contacts"] == [{"name": "Jane"}]


def test_google_places_parser():
    raw = {
        "id": "ChIJ_test_123",
        "displayName": {"text": "Test Senior Care of Tampa", "languageCode": "en"},
        "formattedAddress": "123 Bayshore Blvd, Tampa, FL 33606, USA",
        "addressComponents": [
            {"longText": "Tampa", "shortText": "Tampa", "types": ["locality", "political"]},
            {"longText": "Florida", "shortText": "FL", "types": ["administrative_area_level_1", "political"]},
            {"longText": "33606", "shortText": "33606", "types": ["postal_code"]},
        ],
        "nationalPhoneNumber": "(813) 555-0100",
        "websiteUri": "https://example-senior-care.com",
        "rating": 4.7,
        "userRatingCount": 142,
        "location": {"latitude": 27.93, "longitude": -82.47},
        "regularOpeningHours": {"weekdayDescriptions": ["Monday: 8 AM – 6 PM"]},
        "types": ["health", "establishment", "point_of_interest"],
    }
    pack = load_niche("senior_care_agencies_se")
    biz = _parse_place(raw, "home care agency in Tampa, FL", pack)
    assert biz is not None
    assert biz.id == "ChIJ_test_123"
    assert biz.name == "Test Senior Care of Tampa"
    assert biz.city == "Tampa"
    assert biz.state == "FL"
    assert biz.zip_code == "33606"
    assert biz.phone == "(813) 555-0100"
    assert biz.website == "https://example-senior-care.com"
    assert biz.rating == 4.7
    assert biz.review_count == 142
    assert biz.discovered_via == "home care agency in Tampa, FL"
    # category_label from the niche pack wins over Google's types
    assert biz.category == "senior_care_agency"


def test_google_places_parser_missing_fields():
    """Parser must not crash on partial responses (Google omits fields freely)."""
    raw = {
        "id": "minimal",
        "displayName": {"text": "Minimal Co"},
        "formattedAddress": "Somewhere",
    }
    pack = load_niche("senior_care_agencies_se")
    biz = _parse_place(raw, "x", pack)
    assert biz is not None
    assert biz.id == "minimal"
    assert biz.phone == ""
    assert biz.rating == 0.0


def test_google_places_parser_drops_unnamed():
    pack = load_niche("senior_care_agencies_se")
    assert _parse_place({"id": "x"}, "x", pack) is None
    assert _parse_place({"displayName": {"text": "Y"}}, "x", pack) is None  # no id


def test_outreach_drafter_personalizes():
    pack = load_niche("senior_care_agencies_se")
    business = {
        "id": "b1",
        "name": "Bay Senior Living",
        "city": "Tampa",
        "state": "FL",
        "email": "info@baysenior.com",
        "phone": "(813) 555-0100",
        "website": "https://baysenior.com",
        "contacts": [],
    }
    draft = draft_outreach(business, pack)
    assert draft["business_id"] == "b1"
    assert draft["email"] == "info@baysenior.com"
    assert "Bay Senior Living" in draft["subject"]
    assert "Tampa" in draft["subject"]
    assert "Tampa" in draft["body"]
    # Placeholders are all replaced
    assert "{business_name}" not in draft["body"]
    assert "{city}" not in draft["body"]
    assert "{contact_name}" not in draft["body"]


def test_outreach_drafter_uses_first_contact_name():
    pack = load_niche("senior_care_agencies_se")
    business = {
        "id": "b2",
        "name": "Acme",
        "city": "Orlando",
        "state": "FL",
        "contacts": [{"name": "Janet Smith", "email": "janet@acme.com"}],
    }
    draft = draft_outreach(business, pack)
    assert "Janet Smith" in draft["body"]
    assert draft["email"] == "janet@acme.com"


def test_outreach_drafter_specific_template():
    pack = load_niche("senior_care_agencies_se")
    business = {"id": "b", "name": "X", "city": "Atlanta", "state": "GA"}
    draft = draft_outreach(business, pack, template_key="referral_partner")
    assert draft["template_used"] == "referral_partner"
    assert "referral" in draft["body"].lower() or "Atlanta" in draft["body"]
