"""Auto-drafted second-touch replies for interested cold-outreach recipients.

When the inbound poller classifies an incoming reply as ``interested``,
this module drafts the response we'd send back — referencing what the
recipient said, offering a booking link, a demo video link, and a
written-info fallback. The draft is **queued for operator review**, not
auto-sent. The operator approves or dismisses from the dashboard.

# Why auto-DRAFT, not auto-SEND

The classifier is right ~85% of the time but not 100%. Auto-sending on a
misclassification means an awkward "Thanks for your interest!" reply to
someone who said "Please don't email me." Human review catches that.
Once classifier confidence is well-validated under real volume, we can
move specific niches to auto-send.

# Why one CTA — not three CTAs

The original cold email had one CTA: "Worth 15 minutes?". That worked.
The reply now expands to give the recipient choice:

  1. Book directly: calendar link
  2. Watch first: demo video link
  3. No time?: send a written one-pager

This is fine in a REPLY because the recipient has already engaged. Cold
emails should have one CTA; warm replies should have a primary CTA + 1-2
soft alternates so engaged recipients can self-select.

# Config

Env vars consumed (all optional — gracefully omitted from the draft if unset):

  CWSCRAPER_BOOKING_LINK       — e.g. https://cal.com/checkwellcall/15min
  CWSCRAPER_DEMO_VIDEO_LINK    — e.g. https://loom.com/share/abc
  CWSCRAPER_FROM_NAME          — already used by the SMTP transport
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    anthropic = None  # type: ignore[assignment]
    _ANTHROPIC_AVAILABLE = False

logger = logging.getLogger("cwscraper.auto_reply")

# Same model the personalizer uses — sharing the Anthropic SDK install +
# any prompt-cache benefits from running the same model in the same
# session.
DEFAULT_MODEL = "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are drafting a SECOND-TOUCH email reply for Shaun at CheckWellCall.

# Context

Shaun sent a cold outreach email to {business_name} (a {category} in {city}, {state})
about a referral partnership for daily wellness check calls. The recipient
replied — you'll see their reply text below. Your job is to write the
response Shaun should send back.

# What the response should accomplish

The recipient has expressed interest. You're warming the relationship and
offering them three concrete next-step options, in this order:

  1. **Book a 15-minute call** at: {booking_link}
  2. **Watch a 2-minute walkthrough** at: {demo_video_link}
  3. **Or**: Shaun can send a one-pager with key details if a call doesn't fit right now

If only some of these resources are available (e.g., no demo video link
configured), only mention the ones provided. Never invent URLs.

# How the response should read

- Direct, warm, founder-to-founder. Sound like Shaun.
- 80-120 words total (briefer is better).
- Acknowledge ONE specific thing they said in their reply. Don't summarize
  the whole reply — pick the most concrete detail (a question, an
  observation, a constraint) and respond to it.
- Don't repeat the original pitch. They already read it. This is a
  conversation now.
- End with the three options as a small bulleted list.
- Sign off as Shaun.

# Tone rules

- Never "I appreciate you reaching out!" — it sounds canned
- Never "Looking forward to hearing from you" — it's filler
- Never "We're excited to" — marketing voice
- Use "we" sparingly — it's mostly Shaun talking, not the company
- No emojis. No exclamation marks unless quoting something they said.

# Output format

Just the reply body. Plain text. No subject line, no quotes around it,
no preamble. The reply should be ready to paste into a mail client and
send.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class AutoReplyDraft:
    """One auto-drafted second-touch reply, pending operator approval."""
    business_id: str
    business_name: str
    to_email: str
    subject: str
    body: str
    original_reply_excerpt: str   # what the recipient said — for the operator's context
    booking_link: str
    demo_video_link: str
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "business_id": self.business_id,
            "business_name": self.business_name,
            "to_email": self.to_email,
            "subject": self.subject,
            "body": self.body,
            "original_reply_excerpt": self.original_reply_excerpt[:500],
            "booking_link": self.booking_link,
            "demo_video_link": self.demo_video_link,
            "error": self.error,
        }


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def draft_auto_reply(
    *,
    business: dict,
    reply_text: str,
    reply_subject: str,
    client=None,  # injectable for tests
    model: str | None = None,
) -> AutoReplyDraft:
    """Draft the response Shaun should send to an interested recipient.

    Args:
        business: The matched business prospect (must have id, name, email).
        reply_text: The text body of the recipient's reply.
        reply_subject: The subject line the recipient used (Re: ... typically).
        client: Optional anthropic client (for tests). When omitted,
            constructs one from env.
        model: Override the LLM model. Defaults to claude-haiku-4-5.

    Returns:
        :class:`AutoReplyDraft`. On any failure (no API key, classifier
        crash, empty response), returns a draft with a templated fallback
        body so the operator can still review and send something — and
        the ``error`` field set so they know the AI didn't run.
    """
    booking_link = _env("CWSCRAPER_BOOKING_LINK")
    demo_video_link = _env("CWSCRAPER_DEMO_VIDEO_LINK")
    business_name = (business.get("name") or "").strip() or "your team"
    to_email = (business.get("email") or "").strip()

    # If the contact list has a better email than business.email, prefer that
    contacts = business.get("contacts") or []
    if not to_email and contacts:
        to_email = (contacts[0].get("email") or "").strip()

    # Trim the reply excerpt to ~600 chars — enough context for the LLM
    # without burning tokens on quoted-original-message noise.
    cleaned_reply = _trim_reply_for_prompt(reply_text)

    # Build a deterministic subject — "Re: {their subject}" if their reply
    # didn't already include "Re:"
    subject = reply_subject.strip()
    if subject and not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    elif not subject:
        subject = "Re: your reply"

    # Decide which CTAs we actually have configured
    if not booking_link and not demo_video_link:
        # No CTAs at all — fall back to the simplest reply: ask what would
        # work best for them.
        body = _no_cta_fallback(business_name, cleaned_reply)
        return AutoReplyDraft(
            business_id=business.get("id", ""),
            business_name=business_name,
            to_email=to_email,
            subject=subject,
            body=body,
            original_reply_excerpt=cleaned_reply,
            booking_link="", demo_video_link="",
            error=None,
        )

    # Use the LLM to draft. Falls back to a templated reply if no API key
    # or any failure.
    if client is None:
        if not _ANTHROPIC_AVAILABLE or not os.getenv("ANTHROPIC_API_KEY"):
            return _templated_reply(
                business, business_name, to_email, subject,
                cleaned_reply, booking_link, demo_video_link,
                error="ANTHROPIC_API_KEY not configured — used templated fallback",
            )
        client = anthropic.Anthropic()

    system = SYSTEM_PROMPT.format(
        business_name=business_name,
        category=business.get("category", "senior-care organization").replace("_", " "),
        city=business.get("city", "your area"),
        state=business.get("state", ""),
        booking_link=booking_link or "(none configured)",
        demo_video_link=demo_video_link or "(none configured)",
    )

    user_msg = (
        f"They replied with this:\n\n{cleaned_reply}\n\n"
        f"Write Shaun's response."
    )

    try:
        response = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        logger.warning(f"auto-reply LLM call failed for {business.get('id')}: {e}")
        return _templated_reply(
            business, business_name, to_email, subject,
            cleaned_reply, booking_link, demo_video_link,
            error=f"LLM failed: {e}",
        )

    body = _extract_text(response).strip().strip('"').strip("'")
    # Defensive cleanup — strip any preamble the model might have added
    for prefix in ("Here's the reply:", "Reply:", "Here you go:"):
        if body.lower().startswith(prefix.lower()):
            body = body[len(prefix):].strip()

    if not body:
        return _templated_reply(
            business, business_name, to_email, subject,
            cleaned_reply, booking_link, demo_video_link,
            error="LLM returned empty body — used templated fallback",
        )

    return AutoReplyDraft(
        business_id=business.get("id", ""),
        business_name=business_name,
        to_email=to_email,
        subject=subject,
        body=body,
        original_reply_excerpt=cleaned_reply,
        booking_link=booking_link,
        demo_video_link=demo_video_link,
        error=None,
    )


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

def _templated_reply(
    business: dict, business_name: str, to_email: str, subject: str,
    reply_text: str, booking_link: str, demo_video_link: str,
    *, error: str,
) -> AutoReplyDraft:
    """Static templated reply when the LLM isn't available."""
    options = []
    if booking_link:
        options.append(f"  • Book a 15-min slot: {booking_link}")
    if demo_video_link:
        options.append(f"  • Watch a 2-min walkthrough: {demo_video_link}")
    options.append("  • Or just reply here — happy to send a one-pager if a call doesn't fit")

    body = (
        f"Thanks for the quick reply.\n\n"
        f"Three ways to dig in further — whichever is easiest for {business_name}:\n\n"
        + "\n".join(options) + "\n\n"
        "Best,\nShaun"
    )
    return AutoReplyDraft(
        business_id=business.get("id", ""),
        business_name=business_name,
        to_email=to_email, subject=subject, body=body,
        original_reply_excerpt=reply_text,
        booking_link=booking_link, demo_video_link=demo_video_link,
        error=error,
    )


def _no_cta_fallback(business_name: str, reply_text: str) -> str:
    """When no booking/demo links are configured at all."""
    return (
        f"Thanks for the quick reply.\n\n"
        f"What's the easiest next step for {business_name} — a quick 15-min call, "
        f"or would a brief written overview work better?\n\n"
        "Best,\nShaun"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trim_reply_for_prompt(text: str, max_chars: int = 600) -> str:
    """Strip quoted-original chunks and trim to a sensible size.

    Many replies look like:

        Thanks, that sounds interesting!

        > On Tue, May 21, Shaun wrote:
        > Hi Janet,
        > [200 lines of quoted original]

    We only care about the recipient's NEW text, not their quote of what
    we sent them. Cheap heuristic: stop at the first line starting with
    ``>`` or ``On ... wrote:`` boilerplate.
    """
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if "wrote:" in stripped and (
            stripped.startswith("On ") or stripped.startswith("Le ")
        ):
            break
        out.append(line)
    cleaned = "\n".join(out).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit(" ", 1)[0] + "…"
    return cleaned


def _extract_text(response) -> str:
    """Pull text from an Anthropic Message response. Tolerant of mocks."""
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return " ".join(p for p in parts if p)
