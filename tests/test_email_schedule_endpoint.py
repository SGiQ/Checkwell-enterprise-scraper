"""Regression tests for POST /api/emails/schedule.

Covers the datetime-parsing bug where the dashboard's Date.toISOString()
output (e.g. '2026-05-22T09:14:00.000Z') was being lower-cased before
parsing, breaking the uppercase-'Z' UTC suffix and producing a 400.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Bring up the Flask app pointed at a tmp data dir, return a test client."""
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_NICHE", "caregiver")
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
                "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD",
                "RESEND_API_KEY", "EMAIL_TRANSPORT"):
        monkeypatch.delenv(var, raising=False)

    from cwscraper.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), app


def _payload(**overrides):
    body = {
        "prospect_id": "biz1",
        "lead_type": "business",
        "to_email": "owner@example.com",
        "subject": "Hi",
        "body": "Hello",
    }
    body.update(overrides)
    return body


def test_schedule_accepts_browser_toisostring_format(client):
    """Reproduces the original 400 — Date.prototype.toISOString() in JS emits
    a literal uppercase 'Z' for the UTC offset, and the backend must preserve
    that case before parsing."""
    test_client, _ = client
    resp = test_client.post(
        "/api/emails/schedule",
        json=_payload(scheduled_for="2026-05-22T09:14:00.000Z"),
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["email"]["status"] == "pending"
    # Persisted value should be a parseable ISO timestamp.
    assert "2026-05-22" in body["email"]["scheduled_for"]


def test_schedule_accepts_uppercase_z(client):
    """Explicit upper-case Z suffix — the most common form from browser JS."""
    test_client, _ = client
    resp = test_client.post(
        "/api/emails/schedule",
        json=_payload(scheduled_for="2026-05-22T14:00:00Z"),
    )
    assert resp.status_code == 200


def test_schedule_accepts_naive_iso(client):
    """A naive ISO datetime (no offset) is allowed and treated as UTC."""
    test_client, _ = client
    resp = test_client.post(
        "/api/emails/schedule",
        json=_payload(scheduled_for="2026-05-22T14:00:00"),
    )
    assert resp.status_code == 200


def test_schedule_accepts_offset_form(client):
    """Standard +HH:MM offset must keep working."""
    test_client, _ = client
    resp = test_client.post(
        "/api/emails/schedule",
        json=_payload(scheduled_for="2026-05-22T09:14:00+00:00"),
    )
    assert resp.status_code == 200


def test_schedule_now_alias_is_case_insensitive(client):
    """'now' / 'NOW' / 'Now' should all mean immediate send."""
    test_client, _ = client
    for variant in ("now", "NOW", "Now", "  now  ", ""):
        resp = test_client.post(
            "/api/emails/schedule",
            json=_payload(scheduled_for=variant),
        )
        assert resp.status_code == 200, (variant, resp.get_json())


def test_schedule_rejects_garbage(client):
    """An unparseable string still returns a clear 400."""
    test_client, _ = client
    resp = test_client.post(
        "/api/emails/schedule",
        json=_payload(scheduled_for="not-a-real-date"),
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "Invalid scheduled_for" in body["error"]
    # The 400 message should NOT have been mangled by .lower() — the original
    # input (with whatever case the user sent) should be echoed back.
    assert "not-a-real-date" in body["error"]


def test_schedule_required_fields(client):
    test_client, _ = client
    resp = test_client.post(
        "/api/emails/schedule",
        json={"prospect_id": "biz1", "to_email": "a@b.com"},  # missing subject + body
    )
    assert resp.status_code == 400
    assert "Missing required fields" in resp.get_json()["error"]


def test_schedule_rejects_invalid_email(client):
    test_client, _ = client
    resp = test_client.post(
        "/api/emails/schedule",
        json=_payload(to_email="not-an-email"),
    )
    assert resp.status_code == 400
    assert "Invalid to_email" in resp.get_json()["error"]
