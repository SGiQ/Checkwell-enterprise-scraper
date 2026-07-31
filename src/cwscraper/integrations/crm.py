"""Push scraped business leads to the SGiQ CRM ingestion API.

Best-effort and non-fatal: if the CRM is unreachable or misconfigured we log a
warning and move on — a scan/enrichment run must never fail because of this.

Enable by setting two env vars (see .env.example):
  SGIQ_CRM_URL       e.g. https://sgiq-crm.vercel.app
  SGIQ_CRM_API_KEY   a per-workspace key minted in the CRM (Integrations page)
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, is_dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

_BATCH = 50  # companies do more per-item work (company upsert + nested people)


def _enabled() -> bool:
    return bool(os.getenv("SGIQ_CRM_URL") and os.getenv("SGIQ_CRM_API_KEY"))


def _as_dict(obj: Any) -> dict:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    return {}


# Scraper category slug -> CRM org_type label.
ORG_TYPE_BY_CATEGORY = {
    "pace_program": "PACE",
    "area_agency_on_aging": "Area Agency on Aging",
    "home_health_agency": "Home Health Agency",
    "senior_care_agency": "Senior Care Agency",
    "memory_care_facility": "Memory Care Facility",
    "assisted_living_facility": "Assisted Living Facility",
    "outpatient_clinic": "Outpatient Clinic",
    "mental_health_practice": "Mental Health Practice",
    "geriatric_care_manager": "Geriatric Care Manager",
    "rehab_center": "Rehab Center",
    "church": "Church",
}


def _business_to_company(b: dict) -> dict:
    """Map a BusinessLead to the CRM Company ingestion payload.

    The org becomes a Company; enriched `contacts[]` become the people under it.
    `external_id` is the stable source+id so re-scans merge rather than duplicate.
    """
    people = []
    for c in (b.get("contacts") or []):
        if isinstance(c, dict) and (c.get("name") or c.get("email")):
            people.append({
                "name": c.get("name") or "",
                "title": c.get("title") or None,
                "email": c.get("email") or None,
                "phone": c.get("phone") or None,
            })

    return {
        "name": b.get("name") or "",
        "org_type": ORG_TYPE_BY_CATEGORY.get(b.get("category") or ""),
        "state": b.get("state") or None,
        "metro": b.get("city") or None,
        "website": b.get("website") or None,
        "phone": b.get("phone") or None,
        "email": b.get("email") or None,
        "source": b.get("source") or None,
        "external_id": f"{b.get('source', '')}:{b.get('id', '')}",
        "contacts": people,
        "metadata": {
            "category": b.get("category"),
            "address": b.get("address"),
            "city": b.get("city"),
            "zip_code": b.get("zip_code"),
            "country": b.get("country"),
            "rating": b.get("rating"),
            "review_count": b.get("review_count"),
            "discovered_via": b.get("discovered_via"),
            "source_niches": b.get("source_niches"),
            "scraper_status": b.get("status"),
        },
    }


def push_businesses(businesses: list) -> None:
    """POST scraped businesses to the CRM as Companies, in batches. Never raises.

    (Name kept for the existing engine call sites; payload is now company-shaped.)
    """
    if not _enabled() or not businesses:
        return

    companies = [_business_to_company(_as_dict(b)) for b in businesses]
    companies = [c for c in companies if c.get("name")]  # a Company needs a name
    if not companies:
        return

    url = os.getenv("SGIQ_CRM_URL", "").rstrip("/") + "/api/ingest/companies"
    headers = {
        "X-SGIQ-Key": os.getenv("SGIQ_CRM_API_KEY", ""),
        "Content-Type": "application/json",
    }
    timeout = int(os.getenv("SGIQ_CRM_TIMEOUT", "15"))

    sent = 0
    for i in range(0, len(companies), _BATCH):
        chunk = companies[i : i + _BATCH]
        try:
            resp = requests.post(url, headers=headers, json={"companies": chunk}, timeout=timeout)
            if resp.status_code >= 300:
                log.warning("CRM ingest failed (%s): %s", resp.status_code, resp.text[:300])
            else:
                sent += len(chunk)
        except Exception as e:  # noqa: BLE001 — best-effort, never fatal
            log.warning("CRM ingest error: %s", e)

    if sent:
        log.info("Pushed %d companies to SGiQ CRM", sent)


def notify_email_status(
    entry: dict,
    status: str,
    *,
    provider_id: str = "",
    error: str = "",
) -> None:
    """Tell the CRM the final delivery status of a CRM-originated email.

    Best-effort and non-fatal — a delivery never depends on this. Only fires for
    CRM emails (lead_type 'external' or prospect_id 'crm:<id>'); the CRM matches
    the row by the scraper queue id (stored there as provider_id). Reuses the
    SGIQ_CRM_URL + SGIQ_CRM_API_KEY config; the key needs the 'email:status' scope.
    """
    if not _enabled():
        return
    pid = entry.get("prospect_id", "") or ""
    if entry.get("lead_type") != "external" and not pid.startswith("crm:"):
        return

    url = os.getenv("SGIQ_CRM_URL", "").rstrip("/") + "/api/webhooks/email-status"
    headers = {
        "X-SGIQ-Key": os.getenv("SGIQ_CRM_API_KEY", ""),
        "Content-Type": "application/json",
    }
    timeout = int(os.getenv("SGIQ_CRM_TIMEOUT", "15"))
    payload = {
        "queue_id": entry.get("id", ""),
        "status": status,
        "sent_at": entry.get("sent_at") or "",
        "provider_id": provider_id,
        "error": (error or "")[:300],
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code >= 300:
            log.warning("CRM email-status notify failed (%s): %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        log.warning("CRM email-status notify error: %s", e)


def notify_email_reply(
    *,
    from_email: str,
    classification: str,
    subject: str = "",
    snippet: str = "",
    message_id: str = "",
    received_at: str = "",
) -> None:
    """Forward a classified inbound reply to the CRM.

    Best-effort and non-fatal. The CRM matches the reply to a contact by
    `from_email` within the key's tenant and halts any active campaign drip;
    non-matches are ignored on the CRM side. Reuses the SGIQ_CRM_URL +
    SGIQ_CRM_API_KEY config; the key needs the 'email:replies' scope. Sent for
    every classified reply (scraper prospects AND CRM contacts) — harmless to
    forward one the CRM doesn't recognize.
    """
    if not _enabled():
        return
    addr = (from_email or "").strip().lower()
    if not addr or "@" not in addr:
        return

    url = os.getenv("SGIQ_CRM_URL", "").rstrip("/") + "/api/webhooks/email-reply"
    headers = {
        "X-SGIQ-Key": os.getenv("SGIQ_CRM_API_KEY", ""),
        "Content-Type": "application/json",
    }
    timeout = int(os.getenv("SGIQ_CRM_TIMEOUT", "15"))
    payload = {
        "from_email": addr,
        "classification": classification,
        "subject": subject or "",
        "snippet": (snippet or "")[:4000],  # enough of the reply body for the CRM timeline
        "message_id": message_id or "",
        "received_at": received_at or "",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code >= 300:
            log.warning("CRM reply notify failed (%s): %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        log.warning("CRM reply notify error: %s", e)
