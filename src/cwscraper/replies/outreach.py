"""Cold-email outreach drafter for directory-mode (B2B) leads.

Parallel to community-mode reply drafter — different placeholders,
different templates (subject + body, not just body).

Two personalization layers:
  1. **Template variables** (free, stable): {business_name}, {city}, {state},
     {contact_name}, {website}, {phone}. Substituted at draft time.
  2. **AI opener** (Claude Haiku 4.5, optional): a unique 1-2 sentence
     opener generated per recipient and slotted into the {personalized_opener}
     template variable. Activated when the template uses the variable AND
     a Personalizer instance is passed in. Falls back to a generic regional
     opener if the API is unavailable.

Templates that don't use {personalized_opener} keep working unchanged.
"""
from __future__ import annotations

from cwscraper.core.niche import NichePack, OutreachTemplate
from cwscraper.replies.personalizer import Personalizer, PersonalizationResult


_PERSONALIZED_OPENER_TOKEN = "{personalized_opener}"


def _pick_template(niche: NichePack, template_key: str | None) -> OutreachTemplate | None:
    key = template_key or niche.default_outreach_template
    return niche.outreach_template(key) or (
        niche.outreach_templates[0] if niche.outreach_templates else None
    )


def _personalize(text: str, business: dict, contact_name: str, opener: str = "") -> str:
    """Substitute every supported template variable in one pass."""
    return (
        text
        .replace("{business_name}", business.get("name", "your team"))
        .replace("{city}",          business.get("city", "your area"))
        .replace("{state}",         business.get("state", ""))
        .replace("{contact_name}",  contact_name or "there")
        .replace("{website}",       business.get("website", ""))
        .replace("{phone}",         business.get("phone", ""))
        .replace(_PERSONALIZED_OPENER_TOKEN, opener)
    )


def draft_outreach(
    business: dict,
    niche: NichePack,
    template_key: str | None = None,
    *,
    personalizer: Personalizer | None = None,
) -> dict:
    """Generate a cold-email draft for a business lead.

    Args:
        business: Business dict (id, name, city, state, rating, etc.).
        niche: The active NichePack — provides templates.
        template_key: Which template in the niche to use. Defaults to
            ``niche.default_outreach_template``.
        personalizer: Optional :class:`Personalizer` instance. When the
            chosen template uses ``{personalized_opener}``, the personalizer
            generates a unique opener for this business. Without it, the
            token is replaced by an empty string (template should be
            written to read naturally either way) — keeps drafts working
            offline / without an API key.

    Returns:
        A dict with the rendered subject + body and tracking fields:

            {
              "business_id": str,
              "template_used": str,
              "template_name": str,
              "subject": str,
              "body": str,
              "business_name": str,
              "email": str,
              "phone": str,
              "website": str,
              "personalized_opener": str,    # empty if not used
              "personalization": dict | None  # cost + cache telemetry
            }
    """
    tmpl = _pick_template(niche, template_key)
    if not tmpl:
        return {
            "business_id": business.get("id", ""),
            "template_used": "",
            "template_name": "(no template available)",
            "subject": "",
            "body": "",
            "personalized_opener": "",
            "personalization": None,
        }

    # Pick a contact name: first known contact, else generic.
    contacts = business.get("contacts") or []
    contact_name = (contacts[0].get("name", "") if contacts else "") or ""

    # Generate the AI opener only when the template actually uses it. This
    # keeps cost off for legacy templates (referral_partnership, etc. that
    # were written before {personalized_opener} existed) and lets new
    # templates opt in by adding the token.
    opener = ""
    p_telemetry: dict | None = None
    uses_ai_opener = _PERSONALIZED_OPENER_TOKEN in (tmpl.subject or "") or \
                     _PERSONALIZED_OPENER_TOKEN in (tmpl.body or "")
    if uses_ai_opener and personalizer is not None:
        result: PersonalizationResult = personalizer.personalize(business, niche)
        opener = result.opener
        p_telemetry = result.to_dict()

    return {
        "business_id": business.get("id", ""),
        "template_used": tmpl.key,
        "template_name": tmpl.name,
        "subject": _personalize(tmpl.subject, business, contact_name, opener=opener),
        "body":    _personalize(tmpl.body,    business, contact_name, opener=opener),
        "business_name": business.get("name", ""),
        "email": business.get("email", "") or (contacts[0].get("email", "") if contacts else ""),
        "phone": business.get("phone", ""),
        "website": business.get("website", ""),
        "personalized_opener": opener,
        "personalization": p_telemetry,
    }
