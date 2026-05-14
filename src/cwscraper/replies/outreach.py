"""Cold-email outreach drafter for directory-mode (B2B) leads.

Parallel to community-mode reply drafter — different placeholders,
different templates (subject + body, not just body).
"""
from __future__ import annotations

from cwscraper.core.niche import NichePack, OutreachTemplate


def _pick_template(niche: NichePack, template_key: str | None) -> OutreachTemplate | None:
    key = template_key or niche.default_outreach_template
    return niche.outreach_template(key) or (
        niche.outreach_templates[0] if niche.outreach_templates else None
    )


def _personalize(text: str, business: dict, contact_name: str) -> str:
    return (
        text
        .replace("{business_name}", business.get("name", "your team"))
        .replace("{city}",          business.get("city", "your area"))
        .replace("{state}",         business.get("state", ""))
        .replace("{contact_name}",  contact_name or "there")
        .replace("{website}",       business.get("website", ""))
        .replace("{phone}",         business.get("phone", ""))
    )


def draft_outreach(
    business: dict, niche: NichePack, template_key: str | None = None
) -> dict:
    """Generate a cold-email draft for a business lead."""
    tmpl = _pick_template(niche, template_key)
    if not tmpl:
        return {
            "business_id": business.get("id", ""),
            "template_used": "",
            "template_name": "(no template available)",
            "subject": "",
            "body": "",
        }

    # Pick a contact name: first known contact, else generic.
    contacts = business.get("contacts") or []
    contact_name = (contacts[0].get("name", "") if contacts else "") or ""

    return {
        "business_id": business.get("id", ""),
        "template_used": tmpl.key,
        "template_name": tmpl.name,
        "subject": _personalize(tmpl.subject, business, contact_name),
        "body":    _personalize(tmpl.body,    business, contact_name),
        "business_name": business.get("name", ""),
        "email": business.get("email", "") or (contacts[0].get("email", "") if contacts else ""),
        "phone": business.get("phone", ""),
        "website": business.get("website", ""),
    }
