"""Template-based reply drafting. Templates and triggers come from the niche pack."""
from __future__ import annotations

from cwscraper.core.niche import NichePack, ReplyTemplate


def _pick_template(combined: str, niche: NichePack) -> ReplyTemplate:
    for tmpl in niche.reply_templates:
        if any(trigger in combined for trigger in tmpl.triggers):
            return tmpl
    default = niche.reply_template(niche.default_reply_template)
    if default:
        return default
    if niche.reply_templates:
        return niche.reply_templates[0]
    return ReplyTemplate(
        key="empty", name="Empty", triggers=[], template="(no template configured)"
    )


def _personalize(template_text: str, combined: str) -> str:
    if any(w in combined for w in ["mom", "mother"]):
        parent, parent_name = "mom", "Mom"
    elif any(w in combined for w in ["dad", "father"]):
        parent, parent_name = "dad", "Dad"
    else:
        parent, parent_name = "parent", "your parent"
    return template_text.replace("{parent}", parent).replace("{parent_name}", parent_name)


def draft_reply(lead: dict, niche: NichePack) -> dict:
    """Generate a draft reply for the given lead using the niche's templates."""
    combined = f"{lead.get('title', '')} {lead.get('selftext_preview', '')}".lower()
    tmpl = _pick_template(combined, niche)
    return {
        "lead_id": lead.get("id", ""),
        "template_used": tmpl.key,
        "template_name": tmpl.name,
        "draft_text": _personalize(tmpl.template, combined),
        "parent_url": lead.get("url", ""),
        "source": lead.get("source", ""),
        "post_title": lead.get("title", ""),
    }
