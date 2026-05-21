"""Regression tests for the cold-email polish PR:

  1. {personalized_opener} → empty string no longer leaves a stack of
     blank lines in the rendered body
  2. Niche templates render as paragraphs (folded YAML scalar style),
     no hard mid-sentence line breaks
  3. The /api/outreach/draft endpoint now invokes the personalizer
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cwscraper.core.niche import load_niche, list_bundled_niches
from cwscraper.replies.outreach import draft_outreach
from cwscraper.replies.personalizer import Personalizer


@pytest.fixture
def pace_niche():
    return load_niche("pace_programs_se")


@pytest.fixture
def business():
    return {
        "id": "biz1",
        "name": "Heartland PACE",
        "city": "Greenville", "state": "SC",
        "category": "pace_program",
        "rating": 4.7, "review_count": 56,
    }


# ---------- 1. Empty opener no longer leaves blank-line stack ----------

def test_empty_opener_collapses_blank_lines(pace_niche, business):
    """Template has '\\n\\n{personalized_opener}\\n\\n' around the slot.
    When no personalizer is passed, the token resolves to '' and naively
    produces 4 consecutive newlines. The drafter must collapse those to
    exactly one blank line (\\n\\n) so the email reads naturally."""
    draft = draft_outreach(business, pace_niche, template_key="referral_partnership")
    # No more than two consecutive newlines anywhere in the body
    assert "\n\n\n" not in draft["body"], "found blank-line stack: " + draft["body"]


def test_filled_opener_renders_inline(pace_niche, business):
    """With a real opener filled in, the body has the opener exactly once,
    separated by single blank lines from the greeting and the next
    paragraph."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="OPENER_TEXT_HERE.")],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    p = Personalizer(client=mock_client, api_key="test")
    draft = draft_outreach(
        business, pace_niche, template_key="referral_partnership",
        personalizer=p,
    )
    assert "OPENER_TEXT_HERE." in draft["body"]
    # Opener is between the greeting and the first body paragraph,
    # separated by one blank line on each side — not two.
    assert "\n\n\n" not in draft["body"]


# ---------- 2. Folded YAML — paragraphs don't have mid-sentence breaks ----

def test_pace_template_body_has_no_mid_sentence_line_breaks(pace_niche):
    """The folded YAML scalar style (`body: >`) should produce paragraphs
    that are single lines internally, with blank-line separators only.

    Concretely: no line should start with a lowercase letter (which would
    indicate a sentence-continuation broken across lines)."""
    tmpl = pace_niche.outreach_template("referral_partnership")
    assert tmpl is not None

    for paragraph in tmpl.body.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph or paragraph.startswith("{"):
            continue
        lines = paragraph.split("\n")
        # Allow short lines that are clearly signature blocks (1-3 lines starting
        # with capitals — Shaun / SGiQ ... / 1-877-...)
        if len(lines) <= 3 and all(line.strip()[:1].isupper() or
                                    line.strip()[:1].isdigit()
                                    for line in lines if line.strip()):
            continue
        # Every line within a paragraph must start with a capital, a digit,
        # a quote, or a curly-brace template variable — never with a
        # lowercase letter (which would be a wrapped continuation).
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            first = stripped[0]
            assert first.isupper() or first.isdigit() or first in '"\'{', (
                f"line starts mid-sentence (lowercase first char) "
                f"in template — folded scalar didn't fold:\n  {stripped!r}"
            )


def test_all_directory_niche_bodies_have_unwrapped_paragraphs():
    """Every directory-mode niche template body should have its
    paragraphs unwrapped to single lines (no hard mid-sentence line
    breaks). Signature blocks at the very end of the body are allowed
    to keep their per-line structure since they're metadata, not prose.

    Regression coverage for the polish PR: we previously had YAML
    bodies wrapped to ~75 chars per line, which renders ugly on mobile
    because mail clients re-wrap on top of the existing breaks.
    """
    SIG_STARTERS = ("Best,", "Best regards", "Thanks,", "Regards,", "Sincerely")
    for spec in list_bundled_niches():
        if spec.get("mode") != "directory":
            continue
        niche = load_niche(spec["slug"])
        for tmpl in niche.outreach_templates:
            paragraphs = tmpl.body.split("\n\n")
            for idx, paragraph in enumerate(paragraphs):
                paragraph = paragraph.strip()
                if not paragraph or paragraph.startswith("{") or len(paragraph) < 80:
                    continue
                if "\n" not in paragraph:
                    continue
                # Internal newlines are only OK in a signature block:
                # last paragraph AND first line starts with a signature word
                is_last = idx == len(paragraphs) - 1
                first_line = paragraph.split("\n", 1)[0].strip()
                is_signature = is_last and first_line.startswith(SIG_STARTERS)
                assert is_signature, (
                    f"niche {spec['slug']!r} template {tmpl.key!r} has "
                    f"a non-signature paragraph with internal newlines — "
                    f"unwrap it to a single line:\n{paragraph[:200]}..."
                )


# ---------- 3. /api/outreach/draft now invokes the personalizer ---------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """Bring up the Flask app with the active niche set to PACE."""
    monkeypatch.setenv("CWSCRAPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CWSCRAPER_NICHE", "pace_programs_se")
    for var in (
        "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
        "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD",
        "RESEND_API_KEY", "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    from cwscraper.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), app


def test_single_draft_endpoint_uses_personalizer(client, monkeypatch):
    """The /api/outreach/draft endpoint should consult the personalizer
    on the AppContext, not just render the raw template."""
    test_client, app = client
    ctx = app.extensions["cwscraper"]

    # Seed one business in the repo
    from cwscraper.core.models import BusinessLead
    ctx.repo.add_businesses([
        BusinessLead(
            id="biz1", source="google_places", name="Heartland PACE",
            city="Greenville", state="SC", category="pace_program",
        ),
    ])

    # Replace the personalizer's client with a mock so we can verify
    # it gets called.
    mock_client = MagicMock()
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="MOCK_OPENER_FROM_ENDPOINT.")],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    ctx.personalizer.client = mock_client
    # Force configured=True even without ANTHROPIC_API_KEY in env
    mock_client.api_key = "test"

    resp = test_client.post(
        "/api/outreach/draft",
        json={"business_id": "biz1", "template_key": "referral_partnership"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "MOCK_OPENER_FROM_ENDPOINT." in body["body"]
    assert body["personalized_opener"] == "MOCK_OPENER_FROM_ENDPOINT."
    assert body["personalization"] is not None
    # The personalizer's client was actually invoked
    assert mock_client.messages.create.call_count == 1
