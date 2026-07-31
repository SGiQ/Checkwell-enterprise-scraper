"""AI-personalized opener sentences for cold-outreach emails.

Calls Claude Haiku 4.5 to write 1-2 personal opener sentences for each B2B
business, slotted into the email template via ``{personalized_opener}``.

# Why this exists

The same template fired to 50 businesses with only ``{business_name}`` /
``{city}`` substituted is byte-identical to a spam filter. Personalizing
one sentence per recipient gives each email enough surface-area variation
that bulk-detection heuristics don't fire, and references something the
recipient can plausibly think "huh, they actually looked at us."

# Caching strategy

The expensive part is the **system prompt** — a long, niche-specific
instruction block with brand voice, examples, and constraints. The
**user message** is just the per-business facts (name, city, rating, etc.).

Haiku 4.5 requires a 4096-token minimum prefix for prompt caching to
activate (silently — no error, just no cache hit). The system prompt
below is sized to clear that bar with substantive few-shot examples that
also improve output quality.

For a batch of 25+ businesses in the same niche:
  - First call writes the cache: ~4500 input tokens billed at ~1.25x
  - Subsequent calls read the cache: ~4500 tokens billed at ~0.1x
  - Net cost ~60% lower than uncached calls of equivalent quality

Verify via ``result["cache_read"]`` — if it's zero across a batch,
something invalidated the prefix.

# Cost (approximate, Haiku 4.5 pricing)

  - Per call: ~4500 input + ~50 output tokens
  - Uncached: ~$0.0023 per business
  - With cache: ~$0.0009 per business after the first call in a batch
  - 970 businesses (one niche, single batch): ~$0.87 with cache, ~$2.23 without

# Configuration

Required env var: ``ANTHROPIC_API_KEY``. Optional:
  - ``CWSCRAPER_PERSONALIZER_MODEL`` — override the model id
    (default ``claude-haiku-4-5``)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:  # pragma: no cover — tests can run without the package
    anthropic = None  # type: ignore[assignment]
    _ANTHROPIC_AVAILABLE = False

from apitracker_client import track

from cwscraper.core.niche import NichePack

logger = logging.getLogger("cwscraper.personalizer")

# Always pin the model ID exactly — date suffixes are forbidden, and guessing
# breaks at the next model release. Override via env if needed.
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 120  # 1 sentence ≤30 words ≈ 60 tokens; 120 gives headroom
                          # without leaving room for the model to overflow into
                          # multi-sentence editorial commentary


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

# Brand voice and the "what good looks like" examples are stable across every
# niche. They go in the cached prefix. The niche-specific paragraph + the
# per-business facts are the only volatile parts.

BRAND_VOICE_AND_EXAMPLES = """\
# About CheckWellCall

CheckWellCall is a daily wellness check-call service for older adults living
alone or aging in place. A real person calls each client at their scheduled
time, asks how they're doing, catches early warning signs (missed
medications, falls, cognitive shifts, isolation), and reports back to the
adult-child caregiver. It's not an alarm system, not a medical service —
it's the daily five-minute "I'm okay" call that families used to do
themselves before distance and work made it impossible. The founder is
Shaun, who built this after his own family hit the gap between "Mom is
fine" and "Mom needs assisted living."

# Your job

You write the FIRST sentence of a cold-outreach email going to one B2B
partner in the senior-care space. The email template handles the greeting,
the pitch body, the close, and the signature. Your sentence comes right
after the greeting, as the opener.

Your opener has one job: convince the recipient that this isn't an
automated blast — that someone actually looked at their business before
hitting send. You achieve that by referencing one SPECIFIC, VERIFIABLE
fact about the recipient business that I send you.

# Critical length rule

**ONE sentence. ~30 words max. No exceptions.**

If you can't fit your observation in one sentence under 30 words, cut
something. Brevity is the whole point — the opener earns the recipient's
next ~5 seconds; it isn't the pitch.

# Critical "stay in your lane" rule

Make ONE observation about the business. **STOP.**

Do NOT:
  - Extrapolate consequences of that observation for their customers,
    families, patients, members, or pipeline
  - Use phrases like "which means…", "so probably…", "that suggests…",
    "the families turned away by you…", etc.
  - Diagnose problems the recipient hasn't told you about
  - Tee up the body's argument — the body does that itself

The body of the email already explains the recipient's gap and the
proposed solution. If your opener also previews that, the recipient
reads the same point twice in a row and skims faster. Worse, an
opener that diagnoses the recipient's customer-rejection pipeline lands
slightly defensive — they're being told their org has a problem before
they've finished saying hello.

Observe. Don't editorialize.

# What makes a great opener

You'll receive a structured set of facts. Use them. NEVER invent details.
If I don't send a rating, don't mention reviews. If I don't send a
category, don't guess what they do. Better to be vague but accurate
than specific but wrong.

# Tone

Direct, warm, founder-to-founder. Specific, not aspirational. Never:
  - "I hope this email finds you well"
  - "I am reaching out to..."
  - "We love what you do"
  - "I came across your business..."
  - emojis
  - exclamation marks (unless quoting something)
  - questions (the email body asks the question; the opener observes)

# Output format

Just the sentence. No greeting, no signature, no quote marks, no
"Here's your opener:" preamble. Plain text, one sentence.

# Worked examples

## Example 1 — high reviews, specific category

Input:
- Name: Sunshine Home Care
- City: Sarasota, FL
- Category: home_health_agency
- Rating: 4.9 (243 reviews)

❌ Too long, extrapolates into their funnel:
> Sunshine Home Care's 4.9 across 243 reviews is rare in Sarasota's
> home-health corridor, which means you probably get a lot of referrals
> you can't take on.

❌ Generic:
> I came across Sunshine Home Care in Sarasota and wanted to reach out.

✅ One sentence, observation only, ~25 words:
> Sunshine Home Care's 4.9 across 243 reviews is rare in Sarasota's
> home-health corridor — that kind of consistency is hard to hold under
> demand.

Why it works: references the exact rating, names the specific market,
makes a falsifiable observation about the data itself. Stops there.

## Example 2 — no rating data, just category + city

Input:
- Name: Heartland PACE
- City: Greenville, SC
- Category: pace_program

❌ Extrapolates into their families:
> Heartland PACE is one of only a handful of PACE centers in the Upstate,
> which means you probably field a lot of calls from non-qualifying
> Greenville families.

✅ Stays on the observation:
> Heartland PACE is one of only a handful of PACE centers serving the
> South Carolina Upstate — that's a small universe doing a lot of work.

Why it works: notes a regional fact, characterizes the niche, stops.
Body picks up from here.

## Example 3 — assisted living, mid-tier rating

Input:
- Name: Magnolia Gardens Assisted Living
- City: Macon, GA
- Rating: 4.2 (87 reviews)

✅ Reframes the data without editorializing:
> Magnolia Gardens' 4.2 across 87 reviews in Macon reads more honest than
> the splashy 5.0s small ALFs usually post.

Why it works: takes the rating, contrasts it with a real industry pattern
(small ALFs gaming reviews), implicitly compliments the recipient. One
sentence. No "which means…".

## Example 4 — minimal data (just name + city)

Input:
- Name: Brookside Memory Care
- City: Asheville, NC

✅ Observes the region, stops:
> Asheville keeps getting tagged as one of the SE's emerging memory-care
> markets — Brookside's right in that wave.

Why it works: no rating to work with, so it leans on a real regional
trend and positions the recipient inside it. Single sentence.

## Example 5 — area agency on aging

Input:
- Name: Triangle Area Agency on Aging
- State: NC
- Category: area_agency_on_aging

❌ Too long, claims something about their funnel:
> Triangle AAA sees the full funnel of NC families who need help but
> aren't sure where they fit yet — exactly the families who land on
> CheckWellCall after they discover they're not eligible.

✅ Observation about role, no inferred consequences:
> Triangle AAA is one of the older AAAs in the state — that institutional
> memory is rare in the region.

Why it works: notes a verifiable structural fact (age of the agency),
characterizes its significance, stops. No claim about their families or
funnel.

# Rules of thumb

1. **One sentence. Under 30 words. Always.**
2. Make an observation about THEIR business. Then **stop**.
3. Never use "which means…", "so probably…", "that suggests…", or any
   other inference about their customers, families, members, or pipeline.
4. Anchor on what's verifiable. If unsure, generalize on geography or
   category — never invent specifics.
5. Sound like one founder writing to another at 9pm, not marketing copy.
"""


def _system_prompt_blocks(niche: NichePack) -> list[dict]:
    """Build the cached system prompt as a list with cache_control on the
    final block.

    Layout:
      block 0 (cached)  — brand voice + examples + rules (stable across niches)
      block 1 (cached)  — this niche's specific context (stable within niche)

    The cache_control marker on block 1 caches the entire prefix together.
    Within one batch (same niche), every call after the first reads from
    cache. When the user swaps niches, block 1 changes and the cache rolls
    over to the new niche's prefix.
    """
    niche_context = (
        f"# Today's niche: {niche.display_name}\n\n"
        f"{niche.description.strip()}\n\n"
        f"All businesses I send you in this batch are in this niche. "
        f"Tailor your openers to the role this category plays in the "
        f"senior-care funnel and to the regional context of each business."
    )
    return [
        {"type": "text", "text": BRAND_VOICE_AND_EXAMPLES},
        {
            "type": "text",
            "text": niche_context,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _user_message(business: dict) -> str:
    """Build the per-business user message. Only includes fields that are
    actually populated — keeps the prompt honest about what's known."""
    lines = ["Write an opener for this business. Use only the facts below.\n"]

    name = (business.get("name") or "").strip()
    if name:
        lines.append(f"- Name: {name}")

    city = (business.get("city") or "").strip()
    state = (business.get("state") or "").strip()
    if city and state:
        lines.append(f"- Location: {city}, {state}")
    elif city:
        lines.append(f"- Location: {city}")
    elif state:
        lines.append(f"- Location: {state}")

    category = (business.get("category") or "").strip()
    if category:
        # Convert google_places categories from snake_case to a readable form
        lines.append(f"- Category: {category.replace('_', ' ')}")

    rating = business.get("rating")
    review_count = business.get("review_count") or 0
    if rating and review_count and review_count >= 10:
        lines.append(f"- Rating: {rating} ({review_count} reviews)")

    services = business.get("services") or []
    if services:
        lines.append(f"- Services: {', '.join(str(s) for s in services[:5])}")

    excerpt = (business.get("website_excerpt") or "").strip()
    if excerpt:
        # Trim to ~300 chars to keep token cost down — model only needs a flavor
        if len(excerpt) > 300:
            excerpt = excerpt[:300].rsplit(" ", 1)[0] + "..."
        lines.append(f"- Website note: {excerpt}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class PersonalizationResult:
    """One business's personalization outcome.

    `opener` is the empty string on failure; check `error` to disambiguate.
    `cache_read` / `cache_write` are zero on the first call in a batch
    (writing) and non-zero on subsequent calls (reading).
    """
    business_id: str
    opener: str
    cached_locally: bool       # True if served from the local JSON cache (no API call)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    error: str | None

    def ok(self) -> bool:
        return self.error is None and bool(self.opener)

    def to_dict(self) -> dict:
        return {
            "business_id": self.business_id,
            "opener": self.opener,
            "cached_locally": self.cached_locally,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Personalizer
# ---------------------------------------------------------------------------

class Personalizer:
    """Generates personalized opener sentences via Claude Haiku 4.5.

    The class is safe to reuse across batches; the underlying anthropic
    client handles connection pooling. For batch processing, call
    :meth:`personalize` in a loop or use :meth:`personalize_batch` for the
    sequential convenience wrapper.

    The local on-disk cache (``data/personalizations.json``) lets the bulk
    drafter re-render the same business's draft without re-billing — useful
    when an operator opens the same email modal twice or re-runs a queued
    draft after an SMTP fix.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        cache_file: Path | None = None,
        client=None,  # injectable for tests
    ):
        self.model = model or os.getenv("CWSCRAPER_PERSONALIZER_MODEL", DEFAULT_MODEL)
        self.cache_file = cache_file
        self._cache_lock = threading.RLock()
        self._cache: dict[str, str] = {}
        if cache_file and cache_file.exists():
            try:
                self._cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._cache = {}

        if client is not None:
            self.client = client
        elif _ANTHROPIC_AVAILABLE:
            # anthropic.Anthropic reads ANTHROPIC_API_KEY from env automatically
            self.client = track(anthropic.Anthropic(api_key=api_key), app='checkwell-scraper') if api_key else track(anthropic.Anthropic(), app='checkwell-scraper')
        else:
            self.client = None

    @property
    def configured(self) -> bool:
        """True iff we have a usable Anthropic client + an API key in env."""
        if not _ANTHROPIC_AVAILABLE or self.client is None:
            return False
        return bool(os.getenv("ANTHROPIC_API_KEY") or getattr(self.client, "api_key", None))

    # ---- public API -------------------------------------------------------

    def personalize(
        self,
        business: dict,
        niche: NichePack,
        *,
        use_local_cache: bool = True,
    ) -> PersonalizationResult:
        """Generate an opener for one business.

        Falls back to a generic regional opener (no API call) if the
        personalizer isn't configured — so the bulk drafter can still ship
        emails even when ANTHROPIC_API_KEY is unset.
        """
        biz_id = (business.get("id") or "").strip()

        # 1) Local cache hit — no API call, no cost
        if use_local_cache and biz_id and biz_id in self._cache:
            return PersonalizationResult(
                business_id=biz_id,
                opener=self._cache[biz_id],
                cached_locally=True,
                input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_write_tokens=0,
                error=None,
            )

        # 2) Not configured — return a safe fallback so bulk drafting can proceed
        if not self.configured:
            return PersonalizationResult(
                business_id=biz_id,
                opener=_fallback_opener(business),
                cached_locally=False,
                input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_write_tokens=0,
                error="personalizer not configured (set ANTHROPIC_API_KEY)",
            )

        # 3) Live call
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=_system_prompt_blocks(niche),
                messages=[{"role": "user", "content": _user_message(business)}],
            )
        except Exception as e:  # anthropic.APIError + network errors
            logger.warning("personalize failed for %s: %s", biz_id or "<no-id>", e)
            return PersonalizationResult(
                business_id=biz_id,
                opener=_fallback_opener(business),
                cached_locally=False,
                input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_write_tokens=0,
                error=str(e),
            )

        opener = _extract_text(response).strip().strip('"').strip("'")
        # Defensive cleanup — strip any rogue "Here's your opener:" prefix
        for prefix in ("Here's your opener:", "Opener:", "Here you go:"):
            if opener.lower().startswith(prefix.lower()):
                opener = opener[len(prefix):].strip()

        usage = getattr(response, "usage", None)
        result = PersonalizationResult(
            business_id=biz_id,
            opener=opener,
            cached_locally=False,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            error=None if opener else "empty response",
        )

        # Cache successful results locally
        if opener and biz_id:
            self._cache_put(biz_id, opener)

        return result

    def personalize_batch(
        self,
        businesses: list[dict],
        niche: NichePack,
        *,
        use_local_cache: bool = True,
        progress_cb=None,
    ) -> list[PersonalizationResult]:
        """Sequential batch helper. The Anthropic prompt cache stays warm
        across the batch (5-minute TTL is plenty for ~25–100 businesses).

        ``progress_cb(i, n, result)`` is called after each business if
        provided — used by the bulk drafter to update a progress bar.
        """
        results: list[PersonalizationResult] = []
        n = len(businesses)
        for i, biz in enumerate(businesses, start=1):
            r = self.personalize(biz, niche, use_local_cache=use_local_cache)
            results.append(r)
            if progress_cb is not None:
                try:
                    progress_cb(i, n, r)
                except Exception:
                    logger.exception("progress_cb raised — continuing batch")
        return results

    def forget(self, business_id: str) -> bool:
        """Drop a single business from the local cache so the next
        personalize() call re-rolls the opener."""
        with self._cache_lock:
            if business_id in self._cache:
                del self._cache[business_id]
                self._save_cache()
                return True
            return False

    # ---- internals --------------------------------------------------------

    def _cache_put(self, biz_id: str, opener: str) -> None:
        with self._cache_lock:
            self._cache[biz_id] = opener
            self._save_cache()

    def _save_cache(self) -> None:
        if not self.cache_file:
            return
        try:
            self.cache_file.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("failed to persist personalizer cache: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(response) -> str:
    """Pull the text out of an anthropic Message response, handling the
    block-list shape. Tolerant of mocked responses in tests."""
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return " ".join(p for p in parts if p)


def _fallback_opener(business: dict) -> str:
    """Generic opener used when the API is unavailable or returns nothing.

    Better than an empty string (which would break the template) and
    better than failing the whole bulk draft — the operator can still
    review and tweak before sending."""
    name = (business.get("name") or "your team").strip()
    city = (business.get("city") or "").strip()
    state = (business.get("state") or "").strip()
    if city and state:
        return (
            f"{name} caught our attention as one of the established names "
            f"in {city}, {state} — we wanted to reach out directly."
        )
    if city:
        return f"{name}'s work in {city} caught our attention — wanted to reach out directly."
    return f"{name} caught our attention — wanted to reach out directly."
