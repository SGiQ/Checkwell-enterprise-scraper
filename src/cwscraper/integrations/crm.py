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

_BATCH = 200


def _enabled() -> bool:
    return bool(os.getenv("SGIQ_CRM_URL") and os.getenv("SGIQ_CRM_API_KEY"))


def _as_dict(obj: Any) -> dict:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    return {}


def _business_to_lead(b: dict) -> dict:
    """Map a BusinessLead to the CRM ingestion payload.

    Uses the first enriched contact (if any) as the person; the org name becomes
    the company. `external_id` is the stable source+id so re-scans merge rather
    than duplicate. Everything else is preserved in metadata.
    """
    contacts = b.get("contacts") or []
    primary = contacts[0] if isinstance(contacts, list) and contacts else {}
    name = (primary.get("name") if isinstance(primary, dict) else "") or b.get("name") or ""

    return {
        "name": name,  # the CRM splits this into first/last
        "email": (primary.get("email") if isinstance(primary, dict) else None) or b.get("email") or None,
        "phone": (primary.get("phone") if isinstance(primary, dict) else None) or b.get("phone") or None,
        "company": b.get("name") or None,
        "title": (primary.get("title") if isinstance(primary, dict) else None) or None,
        "lead_source": b.get("source") or None,
        "source": b.get("source") or None,
        "external_id": f"{b.get('source', '')}:{b.get('id', '')}",
        "metadata": {
            "category": b.get("category"),
            "address": b.get("address"),
            "city": b.get("city"),
            "state": b.get("state"),
            "zip_code": b.get("zip_code"),
            "country": b.get("country"),
            "website": b.get("website"),
            "rating": b.get("rating"),
            "review_count": b.get("review_count"),
            "discovered_via": b.get("discovered_via"),
            "source_niches": b.get("source_niches"),
            "contacts": contacts,
            "scraper_status": b.get("status"),
        },
    }


def push_businesses(businesses: list) -> None:
    """POST business leads to the CRM in batches. Never raises."""
    if not _enabled() or not businesses:
        return

    leads = [_business_to_lead(_as_dict(b)) for b in businesses]
    leads = [l for l in leads if l.get("name")]  # CRM requires a name
    if not leads:
        return

    url = os.getenv("SGIQ_CRM_URL", "").rstrip("/") + "/api/ingest/leads"
    headers = {
        "X-SGIQ-Key": os.getenv("SGIQ_CRM_API_KEY", ""),
        "Content-Type": "application/json",
    }
    timeout = int(os.getenv("SGIQ_CRM_TIMEOUT", "15"))

    sent = 0
    for i in range(0, len(leads), _BATCH):
        chunk = leads[i : i + _BATCH]
        try:
            resp = requests.post(url, headers=headers, json=chunk, timeout=timeout)
            if resp.status_code >= 300:
                log.warning("CRM ingest failed (%s): %s", resp.status_code, resp.text[:300])
            else:
                sent += len(chunk)
        except Exception as e:  # noqa: BLE001 — best-effort, never fatal
            log.warning("CRM ingest error: %s", e)

    if sent:
        log.info("Pushed %d business leads to SGiQ CRM", sent)
