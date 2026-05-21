"""Scrape contact emails from a business website.

Strategy:
  1. Fetch the homepage.
  2. Find candidate "contact / about / team" pages.
  3. Visit up to 8 pages total. Extract emails from each via four passes:
       a. mailto: links (name comes free as link text)
       b. Cloudflare-obfuscated emails (data-cfemail="..." attributes)
       c. JSON-LD structured data (schema.org email/contactPoint)
       d. Text-obfuscation patterns (info [at] example [dot] com)
       e. Title-Case name regex near plain emails
       f. Residual emails with no nearby name
  4. Filter junk (image filenames, example.com, noreply@, etc.).
  5. Pick a primary email — prefer personal-looking, fall back to role-based.

Best-effort. Most agency sites yield 0-3 emails. Some yield none (JS-rendered
mailto links — see Playwright for that; not in this module).
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from cwscraper.enrichment.base import BaseEnricher, EnrichmentContext, EnrichmentResult, logger

REQUEST_TIMEOUT = 12
REQUEST_DELAY = 0.5
MAX_PAGES_PER_BUSINESS = 8
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Tight email regex. Word boundaries + standard local/domain rules.
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"
)

# Pair an email with a Title-Case name preceding it (~150 chars before).
# Matches like "John Smith ... john.smith@example.com".
NAME_BEFORE_EMAIL_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})[^@<>]{0,150}?"
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)

# Capture mailto: links with link text — best signal for name <-> email pairing.
MAILTO_LINK_RE = re.compile(
    r'<a[^>]+href=["\']mailto:([^"\'?\s]+)[^"\']*["\'][^>]*>([^<]+)</a>',
    re.IGNORECASE,
)

# Generic <a href="..."> link extraction for contact-page discovery.
LINK_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>',
    re.IGNORECASE,
)

# Junk filters — drop these "emails".
_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "email.com", "domain.com",
    "yourdomain.com", "yoursite.com", "localhost", "test.com",
    "sentry.io", "wixpress.com", "wix.com", "google-analytics.com",
    "googleapis.com", "cloudflare.com", "u.example.com",
}
_JUNK_LOCAL_PARTS = {
    "your", "youremail", "your-email", "name", "user", "username",
}
_ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
)
_ROLE_LOCAL_PARTS = {
    "info", "hello", "contact", "support", "help", "admin", "webmaster",
    "noreply", "no-reply", "donotreply", "do-not-reply", "office",
    "team", "hi", "inquiries", "questions",
}
_NEVER_USEFUL_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "unsubscribe", "bounces", "mailer-daemon", "postmaster",
}

# Patterns in href/link-text that suggest a contact-relevant page.
# Ordered by ranking — earlier entries score higher in the discovery sort.
_CONTACT_LINK_HINTS = (
    "contact", "about", "team", "staff", "leadership",
    "meet-the-team", "our-team", "our-staff", "who-we-are",
    "people", "directors", "owners",
    "management", "principals", "founder", "founders",
    "locations", "office", "offices", "branches",
    "careers", "jobs", "join-our-team",
    "franchise", "franchisees", "franchise-opportunities",
    "press", "media", "newsroom",
)

# ---------------------------------------------------------------------------
# Cloudflare email obfuscation
# Many sites behind Cloudflare wrap emails:
#   <a class="__cf_email__" data-cfemail="abc123..." href="/cdn-cgi/l/email-protection#abc123">[email protected]</a>
# The hex string is XOR-encrypted with the first byte as the key.
# ---------------------------------------------------------------------------
CF_EMAIL_RE = re.compile(
    r'data-cfemail=["\']([a-f0-9]+)["\']',
    re.IGNORECASE,
)


def _decode_cf_email(encoded: str) -> str | None:
    """Decode a Cloudflare-obfuscated email hex string."""
    try:
        key = int(encoded[:2], 16)
        decoded = "".join(
            chr(int(encoded[i:i + 2], 16) ^ key)
            for i in range(2, len(encoded), 2)
        )
        # Sanity-check: decoded must look like an email
        if "@" in decoded and "." in decoded.split("@", 1)[1]:
            return decoded
    except (ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# Text-style obfuscation: "info (at) example (dot) com" / "[at]" / "{at}"
# Conservative — only handles bracketed/parenthesized forms to avoid false
# positives on prose. The bare-word "at" form is too ambiguous to handle
# safely without ML.
# ---------------------------------------------------------------------------
OBFUSCATED_AT_RE = re.compile(
    r"\s*(?:\(\s*at\s*\)|\[\s*at\s*\]|\{\s*at\s*\})\s*",
    re.IGNORECASE,
)
OBFUSCATED_DOT_RE = re.compile(
    r"\s*(?:\(\s*dot\s*\)|\[\s*dot\s*\]|\{\s*dot\s*\})\s*",
    re.IGNORECASE,
)
# Bare-word form: "info at example dot com" — only matches when sandwiched
# between word-like tokens (lookahead/lookbehind for plausible email parts).
WORD_OBFUSCATED_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)\s+at\s+([A-Za-z0-9-]+)(?:\s+dot\s+([A-Za-z0-9-]+))+\b",
    re.IGNORECASE,
)


def _deobfuscate(text: str) -> str:
    """Apply text-obfuscation normalizations so the standard email regex matches."""
    # Bracketed forms — unambiguous, safe to replace globally.
    text = OBFUSCATED_AT_RE.sub("@", text)
    text = OBFUSCATED_DOT_RE.sub(".", text)
    # Word form — replace `info at example dot com` -> `info@example.com`
    def _word_sub(m: re.Match) -> str:
        full = m.group(0)
        # rebuild: local "@" then domain parts joined by "."
        local = m.group(1)
        rest = re.split(r"\s+dot\s+", full[len(local):].lstrip(), flags=re.IGNORECASE)
        # rest[0] starts with " at " — strip the leading "at "
        rest[0] = re.sub(r"^\s*at\s+", "", rest[0], flags=re.IGNORECASE)
        domain = ".".join(part.strip() for part in rest if part.strip())
        return f"{local}@{domain}"
    text = WORD_OBFUSCATED_RE.sub(_word_sub, text)
    return text


# ---------------------------------------------------------------------------
# JSON-LD structured data extraction
# Sites with proper schema.org markup often include email in Organization
# or ContactPoint nodes. Pure-Python parse, no extra dependencies.
# ---------------------------------------------------------------------------
JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _extract_jsonld_emails(html: str) -> list[tuple[str, str]]:
    """Return [(email, name_or_contact_type), ...] from JSON-LD <script> blocks."""
    out: list[tuple[str, str]] = []
    for match in JSONLD_RE.finditer(html):
        raw = match.group(1).strip()
        # Some sites put multiple objects in one block separated by newlines.
        for fragment in _split_jsonld(raw):
            try:
                data = json.loads(fragment)
            except (json.JSONDecodeError, ValueError):
                continue
            out.extend(_walk_jsonld(data, ""))
    return out


def _split_jsonld(raw: str) -> list[str]:
    """Allow JSON-LD blocks that contain multiple top-level objects."""
    raw = raw.strip()
    if raw.startswith("["):
        return [raw]
    # Heuristic: if the block has multiple `{...}` at top level, try each
    decoder = json.JSONDecoder()
    out = []
    idx = 0
    while idx < len(raw):
        # skip whitespace
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw) or raw[idx] != "{":
            break
        try:
            _, end = decoder.raw_decode(raw, idx)
        except (json.JSONDecodeError, ValueError):
            break
        out.append(raw[idx:end])
        idx = end
    return out if out else [raw]


def _walk_jsonld(node, parent_name: str) -> list[tuple[str, str]]:
    """Recursively find email keys in a JSON-LD object/array."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        # `name` or `contactType` is the most likely "who is this email for"
        name = (
            node.get("name")
            or node.get("contactType")
            or parent_name
        )
        if isinstance(name, dict):
            name = name.get("name", "") or ""
        if not isinstance(name, str):
            name = ""

        email = node.get("email")
        if isinstance(email, str):
            out.append((email.strip(), name.strip()))
        elif isinstance(email, list):
            for e in email:
                if isinstance(e, str):
                    out.append((e.strip(), name.strip()))

        for v in node.values():
            if isinstance(v, (dict, list)):
                out.extend(_walk_jsonld(v, name))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_jsonld(item, parent_name))
    return out


class WebsiteScraper(BaseEnricher):
    source = "website"

    def enrich(
        self, business: dict, ctx: EnrichmentContext
    ) -> EnrichmentResult | None:
        url = (business.get("website") or "").strip()
        if not url:
            return None

        # Normalize and guard against weird inputs.
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        if not parsed.netloc:
            return None

        pages_visited: set[str] = set()
        contacts: list[dict] = []
        emails_seen: set[str] = set()

        # Step 1: fetch homepage.
        home_html = self._fetch(url, business, ctx)
        if home_html is None:
            return None
        pages_visited.add(url)
        for c in self._extract_contacts(home_html, url, business):
            if c["email"] not in emails_seen:
                emails_seen.add(c["email"])
                contacts.append(c)

        # Step 2: find candidate contact pages.
        candidates = _discover_contact_pages(home_html, url)

        # Step 3: visit up to MAX_PAGES_PER_BUSINESS pages total.
        for cand_url in candidates:
            if len(pages_visited) >= MAX_PAGES_PER_BUSINESS:
                break
            if cand_url in pages_visited:
                continue
            pages_visited.add(cand_url)
            html = self._fetch(cand_url, business, ctx)
            if not html:
                continue
            for c in self._extract_contacts(html, cand_url, business):
                if c["email"] not in emails_seen:
                    emails_seen.add(c["email"])
                    contacts.append(c)

        if not contacts:
            return EnrichmentResult(source=self.source)

        primary = _pick_primary_email(contacts)
        return EnrichmentResult(
            email=primary,
            contacts=contacts,
            source=self.source,
        )

    # --- internal helpers ---

    def _fetch(self, url: str, business: dict, ctx: EnrichmentContext) -> str | None:
        time.sleep(REQUEST_DELAY)
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            ctx.log_error(business.get("name", url), f"fetch failed: {type(e).__name__}")
            return None
        if resp.status_code >= 400:
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            return None
        return resp.text[:500_000]  # cap memory: 500 KB of HTML is plenty

    def _extract_contacts(
        self, html: str, source_url: str, business: dict
    ) -> list[dict]:
        contacts: list[dict] = []
        seen: set[str] = set()

        def _add(email: str, name: str = "") -> None:
            email = email.strip().lower()
            if _is_junk(email, business) or email in seen:
                return
            seen.add(email)
            contacts.append({"name": name, "email": email, "source_url": source_url})

        # 1. mailto: links — cleanest signal, name as link text.
        for m in MAILTO_LINK_RE.finditer(html):
            link_text = _strip_html(m.group(2)).strip()
            name = link_text if _looks_like_name(link_text) else ""
            _add(m.group(1), name)

        # 2. Cloudflare-obfuscated emails (data-cfemail="...").
        for m in CF_EMAIL_RE.finditer(html):
            decoded = _decode_cf_email(m.group(1))
            if decoded:
                _add(decoded, "")

        # 3. JSON-LD schema.org markup — often has organization email or
        #    ContactPoint entries with role + email.
        for email, name in _extract_jsonld_emails(html):
            _add(email, name if _looks_like_name(name) else "")

        # 4. Strip HTML, apply text-obfuscation normalization, then regex.
        text = _strip_html(html)
        text = _deobfuscate(text)

        # 4a. Title-Case name preceding email.
        for m in NAME_BEFORE_EMAIL_RE.finditer(text):
            _add(m.group(2), m.group(1).strip())

        # 4b. Residual emails with no nearby name.
        for m in EMAIL_RE.finditer(text):
            _add(m.group(0))

        return contacts


# --- module-level helpers (pure-fn, easy to test) ---

def _strip_html(html: str) -> str:
    """Crude HTML → text. Good enough for email extraction."""
    no_script = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", " ", no_script)
    return re.sub(r"\s+", " ", no_tags)


# Words that — when they appear in extracted "contact name" candidates —
# almost always indicate a UI widget label, button text, cookie/privacy
# banner, or other non-person string scraped from a contact page.
# False positive cost: a real name like "Skip Johnson" gets filtered out and
# we fall back to "Hi there," in the email. Acceptable price to never send
# "Hi Accessibility Tools Accessibility," to a real prospect.
_NAME_JUNK_WORDS: frozenset[str] = frozenset({
    # Accessibility-overlay widgets (the bug that prompted this filter)
    "accessibility", "accessible", "wcag",
    # Cookie / privacy / consent banners
    "cookie", "cookies", "privacy", "policy", "consent", "gdpr", "ccpa",
    "preferences", "settings",
    # Site navigation / skip links
    "skip", "menu", "navigation", "main", "content", "header", "footer",
    "sidebar", "toggle",
    # Generic calls-to-action / button text
    "subscribe", "newsletter", "sign", "signup", "register", "login",
    "logout", "download", "click", "learn", "more", "read", "submit",
    "next", "previous", "back",
    # Site sections / pages
    "home", "page", "search", "products", "services", "categories",
    "blog", "news", "events",
    # Marketing / legal boilerplate
    "terms", "service", "legal", "disclaimer", "copyright", "all", "rights",
    "reserved",
    # Common contact-page widget labels (not person names)
    "tools", "widget", "button", "form", "field",
})


def _looks_like_name(text: str) -> bool:
    """Heuristic: does this string plausibly look like a contact's name?

    Returns False for widget labels (Accessibility Tools), navigation
    elements (Skip Content, Read More), CTAs (Subscribe Now), and other
    junk strings that share the multi-word-title-case shape with real
    names but aren't.
    """
    if not text or len(text) > 60:
        return False
    if "@" in text or "/" in text:
        return False
    parts = text.split()
    if not (2 <= len(parts) <= 4):
        return False
    if not all(p[:1].isupper() for p in parts if p):
        return False
    # Reject if any word looks like UI / boilerplate text
    lower_words = [p.lower() for p in parts]
    if any(w in _NAME_JUNK_WORDS for w in lower_words):
        return False
    # Reject if any word repeats — real names don't have "Smith Smith"
    # (catches the original "Accessibility Tools Accessibility" pattern
    # even if the denylist somehow misses it)
    if len(set(lower_words)) != len(lower_words):
        return False
    return True


def _is_junk(email: str, business: dict) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain:
        return True
    if any(email.lower().endswith(ext) for ext in _ASSET_EXTENSIONS):
        return True
    if domain.lower() in _JUNK_DOMAINS:
        return True
    if local.lower() in _JUNK_LOCAL_PARTS:
        return True
    if local.lower() in _NEVER_USEFUL_LOCAL_PARTS:
        return True
    return False


def _pick_primary_email(contacts: list[dict]) -> str:
    """Prefer personal-looking emails, then role emails, then anything."""
    if not contacts:
        return ""
    personals = [c for c in contacts if c["email"].split("@")[0].lower() not in _ROLE_LOCAL_PARTS]
    if personals:
        # Prefer one that has a name attached.
        named = [c for c in personals if c.get("name")]
        return (named[0] if named else personals[0])["email"]
    return contacts[0]["email"]


def _discover_contact_pages(home_html: str, base_url: str) -> list[str]:
    """Find contact/about/team links on the homepage. Same-origin only."""
    base_origin = _origin(base_url)
    found: list[tuple[int, str]] = []  # (rank, url) — lower rank = more relevant
    seen: set[str] = set()

    for m in LINK_RE.finditer(home_html):
        href, text = m.group(1), m.group(2)
        href = href.strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        if _origin(absolute) != base_origin:
            continue  # same-origin only
        if absolute in seen:
            continue
        seen.add(absolute)
        rank = _rank_contact_candidate(absolute, _strip_html(text))
        if rank is not None:
            found.append((rank, absolute))

    found.sort(key=lambda x: x[0])
    return [u for _, u in found[:6]]  # cap discovery output; visitor loop caps actual fetches


def _rank_contact_candidate(url: str, link_text: str) -> int | None:
    """Lower rank = more relevant. None = not a candidate."""
    haystack = (url + " " + link_text).lower()
    best: int | None = None
    for i, hint in enumerate(_CONTACT_LINK_HINTS):
        if hint in haystack:
            score = i  # earlier hints rank higher
            if best is None or score < best:
                best = score
    return best


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()
