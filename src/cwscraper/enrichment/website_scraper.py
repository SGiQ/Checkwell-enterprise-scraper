"""Scrape contact emails from a business website.

Strategy:
  1. Fetch the homepage.
  2. Find candidate "contact / about / team" pages.
  3. Visit up to 4 pages total. Extract emails from each.
  4. Pair emails with nearby names when possible (mailto link text is gold).
  5. Filter junk (image filenames, example.com, noreply@, etc.).
  6. Pick a primary email — prefer personal-looking, fall back to role-based.

Best-effort. Most agency sites yield 0-3 emails. Some yield none (forms only).
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlparse

import requests

from cwscraper.enrichment.base import BaseEnricher, EnrichmentContext, EnrichmentResult, logger

REQUEST_TIMEOUT = 12
REQUEST_DELAY = 0.5
MAX_PAGES_PER_BUSINESS = 4
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
_CONTACT_LINK_HINTS = (
    "contact", "about", "team", "staff", "leadership",
    "meet-the-team", "our-team", "our-staff", "who-we-are",
    "people", "directors", "owners",
)


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

        # 1. mailto: links are the cleanest — name comes free as link text.
        for m in MAILTO_LINK_RE.finditer(html):
            email = m.group(1).strip().lower()
            link_text = _strip_html(m.group(2)).strip()
            name = link_text if _looks_like_name(link_text) else ""
            if _is_junk(email, business) or email in seen:
                continue
            seen.add(email)
            contacts.append({"name": name, "email": email, "source_url": source_url})

        # 2. name-near-email regex on plain text.
        text = _strip_html(html)
        for m in NAME_BEFORE_EMAIL_RE.finditer(text):
            name, email = m.group(1).strip(), m.group(2).strip().lower()
            if _is_junk(email, business) or email in seen:
                continue
            seen.add(email)
            contacts.append({"name": name, "email": email, "source_url": source_url})

        # 3. residual emails with no nearby name.
        for m in EMAIL_RE.finditer(text):
            email = m.group(0).strip().lower()
            if _is_junk(email, business) or email in seen:
                continue
            seen.add(email)
            contacts.append({"name": "", "email": email, "source_url": source_url})

        return contacts


# --- module-level helpers (pure-fn, easy to test) ---

def _strip_html(html: str) -> str:
    """Crude HTML → text. Good enough for email extraction."""
    no_script = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", " ", no_script)
    return re.sub(r"\s+", " ", no_tags)


def _looks_like_name(text: str) -> bool:
    if not text or len(text) > 60:
        return False
    if "@" in text or "/" in text:
        return False
    parts = text.split()
    if not (2 <= len(parts) <= 4):
        return False
    return all(p[:1].isupper() for p in parts if p)


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
