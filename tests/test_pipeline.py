"""Tests for pipeline / CRM-lite layer."""
from __future__ import annotations

from pathlib import Path

import pytest

from cwscraper.core.models import (
    LEGACY_STATUS_TO_STAGE,
    PIPELINE_STAGES,
    BusinessLead,
    Lead,
)
from cwscraper.core.store import JSONRepository, _diff_summary, _ensure_pipeline_fields


@pytest.fixture
def repo(tmp_path: Path) -> JSONRepository:
    return JSONRepository(data_dir=tmp_path)


# ----- migration / backfill ------------------------------------------------

def test_legacy_status_maps_to_stage():
    assert LEGACY_STATUS_TO_STAGE["new"]       == "new"
    assert LEGACY_STATUS_TO_STAGE["reviewed"]  == "qualified"
    assert LEGACY_STATUS_TO_STAGE["qualified"] == "qualified"
    assert LEGACY_STATUS_TO_STAGE["contacted"] == "outreach_sent"
    assert LEGACY_STATUS_TO_STAGE["dismissed"] == "lost"


def test_ensure_pipeline_fields_backfills_from_legacy_status():
    row = {"id": "x", "status": "contacted"}
    _ensure_pipeline_fields(row)
    assert row["pipeline_stage"] == "outreach_sent"
    assert row["notes"] == ""
    assert row["follow_up_date"] == ""
    assert row["tags"] == []
    assert row["activity_log"] == []


def test_ensure_pipeline_fields_respects_existing_stage():
    row = {"id": "x", "status": "new", "pipeline_stage": "meeting_booked"}
    _ensure_pipeline_fields(row)
    assert row["pipeline_stage"] == "meeting_booked"  # not overwritten


# ----- unified prospects view ---------------------------------------------

def test_get_all_prospects_unifies_both_types(repo):
    repo.add_leads([Lead(id="L1", title="caregiver post", source="r/AgingParents")])
    repo.add_businesses([BusinessLead(id="B1", name="Tampa Bay Home Care", city="Tampa", state="FL")])

    prospects = repo.get_all_prospects()
    assert len(prospects) == 2

    types = {p["lead_type"] for p in prospects}
    assert types == {"community", "business"}

    # Every prospect has pipeline fields filled in
    for p in prospects:
        assert "pipeline_stage" in p
        assert "notes" in p
        assert "tags" in p
        assert "activity_log" in p


def test_get_all_prospects_migrates_old_status(repo):
    # Pretend an old leads.json row exists with only `status`, no pipeline_stage
    import json
    repo.leads_file.write_text(json.dumps([{
        "id": "old1", "title": "x", "status": "contacted",  # legacy row
    }]), encoding="utf-8")

    prospects = repo.get_all_prospects()
    assert prospects[0]["pipeline_stage"] == "outreach_sent"


# ----- update_prospect ----------------------------------------------------

def test_update_prospect_changes_stage_and_logs_activity(repo):
    repo.add_businesses([BusinessLead(id="B1", name="Acme")])
    result = repo.update_prospect(
        "B1", "business", {"pipeline_stage": "qualified"}, action="stage_change"
    )
    assert result is not None
    assert result["pipeline_stage"] == "qualified"
    assert len(result["activity_log"]) == 1
    entry = result["activity_log"][0]
    assert entry["action"] == "stage_change"
    assert "qualified" in entry["detail"]
    assert "ts" in entry


def test_update_prospect_returns_none_for_unknown_id(repo):
    repo.add_businesses([BusinessLead(id="B1", name="Acme")])
    assert repo.update_prospect("nope", "business", {"notes": "x"}, action="x") is None


def test_update_prospect_targets_correct_lead_type(repo):
    repo.add_leads([Lead(id="X", title="post")])
    repo.add_businesses([BusinessLead(id="X", name="biz with same id")])
    # Same id in both tables — must NOT cross over
    result = repo.update_prospect("X", "community", {"notes": "lead note"}, action="notes_updated")
    assert result is not None
    assert result["title"] == "post"
    # Business shouldn't have been touched
    biz = repo.get_businesses()[0]
    assert biz.get("notes", "") == ""


def test_update_prospect_appends_activity_log_across_multiple_edits(repo):
    repo.add_businesses([BusinessLead(id="B1", name="Acme")])
    repo.update_prospect("B1", "business", {"pipeline_stage": "qualified"}, action="stage_change")
    repo.update_prospect("B1", "business", {"notes": "called Janet"}, action="notes_updated")
    repo.update_prospect("B1", "business", {"follow_up_date": "2026-06-01"}, action="follow_up_set")

    biz = repo.get_businesses()[0]
    assert len(biz["activity_log"]) == 3
    actions = [a["action"] for a in biz["activity_log"]]
    assert actions == ["stage_change", "notes_updated", "follow_up_set"]


def test_update_prospect_preserves_other_fields(repo):
    repo.add_businesses([BusinessLead(
        id="B1", name="Acme", city="Tampa", state="FL", phone="(813) 555-0100"
    )])
    repo.update_prospect("B1", "business", {"pipeline_stage": "qualified"}, action="stage_change")
    biz = repo.get_businesses()[0]
    assert biz["name"] == "Acme"
    assert biz["city"] == "Tampa"
    assert biz["phone"] == "(813) 555-0100"


# ----- diff summary -------------------------------------------------------

def test_diff_summary_stage_change():
    row = {"pipeline_stage": "qualified"}
    assert "qualified -> outreach_sent" in _diff_summary(row, {"pipeline_stage": "outreach_sent"})


def test_diff_summary_notes_redacted():
    # Notes content shouldn't be in the activity log — too verbose
    row = {"notes": "old"}
    assert _diff_summary(row, {"notes": "new note"}) == "notes updated"


def test_diff_summary_tags():
    row = {"tags": []}
    assert "tags=" in _diff_summary(row, {"tags": ["hot-lead"]})


def test_diff_summary_noop():
    row = {"pipeline_stage": "new"}
    assert _diff_summary(row, {"pipeline_stage": "new"}) == "no-op"


# ----- stage constants ---------------------------------------------------

def test_pipeline_stages_contains_expected_workflow():
    assert "new" in PIPELINE_STAGES
    assert "qualified" in PIPELINE_STAGES
    assert "outreach_sent" in PIPELINE_STAGES
    assert "reply_received" in PIPELINE_STAGES
    assert "meeting_booked" in PIPELINE_STAGES
    assert "customer" in PIPELINE_STAGES
    assert "lost" in PIPELINE_STAGES
