"""Regression tests for PR: tighten AI opener + fix 'Hi there,' fallback.

# Why this exists

A real cold email shipped through the dashboard on 2026-05-21 to a PACE
program in Norcross opened with:

    Hi there,

    First Senior Center's 5.0 across 323 reviews is uncommon for any
    senior-care program, let alone a PACE center managing the complexity
    of nursing-home-level participants — which means the families turned
    away for income or service-area reasons are probably just as stuck
    as ever.

Two distinct problems:

1. **"Hi there,"** — the contact name slot fell back to a generic string
   because the website scraper didn't find a plausible name (the new
   junk filter from PR #6 correctly rejected the candidate). The
   fallback should use the business name we always have.

2. **Opener overlap** — the AI opener's tail ("which means the families
   turned away…") previewed the body's first paragraph verbatim AND
   editorialized about the recipient's customer-rejection pipeline.
   Both are bad: redundant + slightly defensive lean.

These tests pin both fixes so they don't regress.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cwscraper.core.niche import load_niche
from cwscraper.replies.outreach import _greeting_fallback, draft_outreach
from cwscraper.replies.personalizer import (
    BRAND_VOICE_AND_EXAMPLES,
    DEFAULT_MAX_TOKENS,
    Personalizer,
)


# ---------------------------------------------------------------------------
# Greeting fallback
# ---------------------------------------------------------------------------

def test_greeting_fallback_uses_business_name():
    assert _greeting_fallback({"name": "First Senior Center"}) == "First Senior Center team"


def test_greeting_fallback_strips_whitespace():
    assert _greeting_fallback({"name": "  Acme Care  "}) == "Acme Care team"


def test_greeting_fallback_handles_missing_business_name():
    """Defensive — every scraped business has a name, but if one slips
    through without one, render a clean fallback rather than crashing."""
    assert _greeting_fallback({}) == "team"
    assert _greeting_fallback({"name": ""}) == "team"
    assert _greeting_fallback({"name": None}) == "team"


def test_draft_outreach_renders_business_name_greeting_when_no_contact():
    """End-to-end: a business with no contacts gets 'Hi {business} team,' —
    not 'Hi there,'. This is the actual regression we shipped today."""
    niche = load_niche("pace_programs_se")
    biz = {
        "id": "biz1",
        "name": "First Senior Center",
        "city": "Norcross", "state": "GA",
        "category": "pace_program",
        "rating": 5.0, "review_count": 323,
        # No contacts[] — this is the actual data shape that triggered the bug
    }
    draft = draft_outreach(biz, niche, template_key="referral_partnership")
    assert "Hi First Senior Center team," in draft["body"]
    # Sanity: the old fallback is gone
    assert "Hi there," not in draft["body"]


def test_draft_outreach_renders_real_contact_when_present():
    """When the contacts[] list has a real name that passes the filter,
    use it — the greeting fallback only kicks in when contact_name is empty."""
    niche = load_niche("pace_programs_se")
    biz = {
        "id": "biz1",
        "name": "First Senior Center",
        "city": "Norcross", "state": "GA",
        "contacts": [{"name": "Janet Smith", "email": "janet@first.example"}],
    }
    draft = draft_outreach(biz, niche, template_key="referral_partnership")
    assert "Hi Janet Smith," in draft["body"]
    assert "Hi First Senior Center team," not in draft["body"]


def test_draft_outreach_renders_business_team_when_contacts_have_empty_name():
    """Contacts list exists but the first contact's name is empty (filtered
    out by the junk-name scrub from PR #7). Fall back to business-name
    team — that's the realistic post-scrub state."""
    niche = load_niche("pace_programs_se")
    biz = {
        "id": "biz1",
        "name": "First Senior Center",
        "city": "Norcross", "state": "GA",
        "contacts": [{"name": "", "email": "info@first.example"}],
    }
    draft = draft_outreach(biz, niche, template_key="referral_partnership")
    assert "Hi First Senior Center team," in draft["body"]


# ---------------------------------------------------------------------------
# Personalizer system prompt — must contain the new constraints
# ---------------------------------------------------------------------------

def test_prompt_enforces_one_sentence():
    """The prompt must explicitly say ONE sentence, not '1-2 sentences'."""
    assert "ONE sentence" in BRAND_VOICE_AND_EXAMPLES or "One sentence" in BRAND_VOICE_AND_EXAMPLES
    # Old language gone
    assert "1-2 sentences" not in BRAND_VOICE_AND_EXAMPLES
    assert "1-2 SENTENCES" not in BRAND_VOICE_AND_EXAMPLES


def test_prompt_enforces_word_cap():
    """The prompt must specify the ~30-word cap explicitly."""
    assert "30 words" in BRAND_VOICE_AND_EXAMPLES


def test_prompt_bans_extrapolation_phrases():
    """The prompt must explicitly ban 'which means…' and related inference
    patterns that caused the original bug ('which means the families turned
    away are probably just as stuck as ever')."""
    # The banned phrases are listed in the prompt as anti-patterns
    assert "which means" in BRAND_VOICE_AND_EXAMPLES.lower()
    # 'so probably' was the other repeated failure mode
    assert "so probably" in BRAND_VOICE_AND_EXAMPLES.lower() or \
           "that suggests" in BRAND_VOICE_AND_EXAMPLES.lower()


def test_prompt_has_stay_in_your_lane_rule():
    """The prompt must explicitly direct the model NOT to extrapolate
    consequences for customers/families/pipeline."""
    prompt_lower = BRAND_VOICE_AND_EXAMPLES.lower()
    # At least one of these should appear — they each frame the rule
    assert any(phrase in prompt_lower for phrase in (
        "stay in your lane",
        "do not editorialize",
        "observe. don't editorialize",
        "make an observation",
    ))


def test_max_tokens_tightened():
    """With a 1-sentence ~30-word cap, max_tokens shouldn't be high enough
    that the model can drift into 2+ sentences of editorial commentary."""
    assert DEFAULT_MAX_TOKENS <= 150, (
        f"DEFAULT_MAX_TOKENS={DEFAULT_MAX_TOKENS} — too high; "
        f"model will overflow the one-sentence cap"
    )


# ---------------------------------------------------------------------------
# Drafter still works end-to-end with the tightened prompt
# ---------------------------------------------------------------------------

def test_draft_outreach_still_renders_opener_when_personalizer_succeeds():
    """Regression: tightening the prompt shouldn't break the integration —
    the AI opener still lands between the greeting and the body."""
    niche = load_niche("pace_programs_se")
    biz = {
        "id": "biz1",
        "name": "First Senior Center",
        "city": "Norcross", "state": "GA",
        "rating": 5.0, "review_count": 323,
    }
    mock = MagicMock()
    mock.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(
            type="text",
            text="First Senior Center's 5.0 across 323 reviews stands out in the SE PACE landscape.",
        )],
        usage=SimpleNamespace(
            input_tokens=200, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    p = Personalizer(client=mock, api_key="test")
    draft = draft_outreach(
        biz, niche, template_key="referral_partnership", personalizer=p,
    )
    assert "First Senior Center team" in draft["body"]
    assert "stands out in the SE PACE landscape" in draft["body"]
    # The opener appears between the greeting and the first body paragraph
    greeting_idx = draft["body"].find("Hi First Senior Center team,")
    opener_idx = draft["body"].find("stands out in the SE PACE landscape")
    pitch_idx = draft["body"].find("You probably get several calls")
    assert greeting_idx < opener_idx < pitch_idx
