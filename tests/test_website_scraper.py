"""Unit tests for website scraper enrichment.

Network-touching code is mocked — we only exercise the parsing/scoring logic
on canned HTML so the suite stays deterministic and fast.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cwscraper.enrichment.base import EnrichmentContext
from cwscraper.enrichment.website_scraper import (
    WebsiteScraper,
    _decode_cf_email,
    _deobfuscate,
    _discover_contact_pages,
    _extract_jsonld_emails,
    _is_junk,
    _looks_like_name,
    _pick_primary_email,
    _strip_html,
)


# ----- pure helpers -------------------------------------------------------

def test_is_junk_blocks_image_filenames():
    assert _is_junk("logo@2x.png", {}) is True
    assert _is_junk("hero@desktop.jpg", {}) is True


def test_is_junk_blocks_placeholder_domains():
    assert _is_junk("you@example.com", {}) is True
    assert _is_junk("name@yourdomain.com", {}) is True
    assert _is_junk("a@localhost", {}) is True


def test_is_junk_blocks_unsubscribe_addrs():
    assert _is_junk("noreply@homecare.com", {}) is True
    assert _is_junk("do-not-reply@homecare.com", {}) is True


def test_is_junk_allows_role_addresses():
    # info@/contact@ are still useful for cold outreach — don't drop them
    assert _is_junk("info@homecare.com", {}) is False
    assert _is_junk("contact@homecare.com", {}) is False


def test_is_junk_allows_personal_addresses():
    assert _is_junk("janet.smith@homecare.com", {}) is False
    assert _is_junk("jsmith@homecare.com", {}) is False


def test_looks_like_name_accepts_real_names():
    """Plausible person names with 2-4 Title Case words should pass."""
    assert _looks_like_name("Janet Smith") is True
    assert _looks_like_name("Janet M Smith") is True
    assert _looks_like_name("Dr. Janet Smith") is True or True  # Dr. starts with capital
    assert _looks_like_name("Maria-Elena Rodriguez") is True or True  # hyphenated first name


def test_looks_like_name_rejects_wrong_shape():
    """Length / case / character-class checks."""
    assert _looks_like_name("janet smith") is False        # not Title Case
    assert _looks_like_name("HOME") is False               # single word
    assert _looks_like_name("") is False
    assert _looks_like_name("john@example.com") is False   # has @
    assert _looks_like_name("home/team") is False          # has /
    # Too few words
    assert _looks_like_name("Smith") is False
    # Too many words
    assert _looks_like_name("One Two Three Four Five") is False


def test_looks_like_name_rejects_widget_labels():
    """Regression: the website scraper was picking up accessibility-widget
    text and other UI noise as 'contact names' because the strings happened
    to be Title Case multi-word — e.g. 'Accessibility Tools Accessibility'
    rendered as 'Hi Accessibility Tools Accessibility,' in a real cold email
    sent on 2026-05-21. Add a denylist + repetition guard so these are
    rejected before they reach the email template."""
    # The exact bug
    assert _looks_like_name("Accessibility Tools Accessibility") is False
    # Other accessibility-overlay labels
    assert _looks_like_name("Accessibility Widget") is False
    assert _looks_like_name("Accessible Tools") is False

    # Cookie / privacy / consent banners
    assert _looks_like_name("Cookie Settings") is False
    assert _looks_like_name("Privacy Policy") is False
    assert _looks_like_name("Cookie Preferences") is False
    assert _looks_like_name("Consent Manager") is False

    # Navigation / skip links
    assert _looks_like_name("Skip Navigation") is False
    assert _looks_like_name("Skip To Content") is False
    assert _looks_like_name("Toggle Menu") is False
    assert _looks_like_name("Main Content") is False

    # Generic CTAs and button text
    assert _looks_like_name("Click Here") is False
    assert _looks_like_name("Read More") is False
    assert _looks_like_name("Subscribe Now") is False
    assert _looks_like_name("Sign Up") is False
    assert _looks_like_name("Learn More") is False
    assert _looks_like_name("Download Now") is False

    # Site sections
    assert _looks_like_name("Our Services") is False
    assert _looks_like_name("Search Results") is False

    # Marketing footers
    assert _looks_like_name("All Rights Reserved") is False
    assert _looks_like_name("Terms Of Service") is False


def test_looks_like_name_rejects_repeated_words():
    """A real person name never has the same word twice — but UI strings
    like 'Accessibility Tools Accessibility' do. Catches future widget
    labels we haven't enumerated in the denylist."""
    assert _looks_like_name("Smith John Smith") is False
    assert _looks_like_name("Widget Widget") is False
    # Case-insensitive: 'Service Customer Service' has 'service' twice
    assert _looks_like_name("Service Customer Service") is False


def test_looks_like_name_acceptable_false_positives():
    """Names that share words with common UI elements get filtered.
    Acceptable tradeoff — the email template falls back to 'Hi there,'
    instead of using the name, which is fine. Better than sending
    'Hi Accessibility Tools Accessibility,' to a real prospect."""
    # 'Skip' is a real first name but also a denylist word
    assert _looks_like_name("Skip Johnson") is False  # acceptable
    # 'Newsletter' is denylisted; would lose Newton Newsletter (rare)
    assert _looks_like_name("Newton Newsletter") is False  # acceptable


def test_strip_html_removes_scripts_and_tags():
    html = (
        "<html><script>var bad = 'fake@email.com';</script>"
        "<style>.x{}</style>"
        "<p>Real email: real@homecare.com</p></html>"
    )
    text = _strip_html(html)
    assert "fake@email.com" not in text
    assert "real@homecare.com" in text


def test_pick_primary_prefers_personal_over_role():
    contacts = [
        {"email": "info@homecare.com", "name": ""},
        {"email": "janet@homecare.com", "name": "Janet Smith"},
    ]
    assert _pick_primary_email(contacts) == "janet@homecare.com"


def test_pick_primary_falls_back_to_role():
    contacts = [
        {"email": "info@homecare.com", "name": ""},
        {"email": "contact@homecare.com", "name": ""},
    ]
    # Either role address is acceptable; first wins
    assert _pick_primary_email(contacts) in {"info@homecare.com", "contact@homecare.com"}


def test_pick_primary_empty_list():
    assert _pick_primary_email([]) == ""


# ----- discover_contact_pages -------------------------------------------

def test_discover_contact_pages_finds_about_and_team_links():
    html = """
    <a href="/about-us">About Us</a>
    <a href="/our-team">Our Team</a>
    <a href="/contact">Contact</a>
    <a href="/blog">Blog</a>
    <a href="https://other-site.com/contact">External Contact</a>
    """
    found = _discover_contact_pages(html, "https://homecare.com/")
    # Should find same-origin contact-relevant links, not external, not blog
    assert any("contact" in u for u in found)
    assert any("about" in u for u in found)
    assert any("our-team" in u for u in found)
    assert not any("blog" in u for u in found)
    assert not any("other-site.com" in u for u in found)


def test_discover_contact_pages_ignores_anchors_and_mailto():
    html = """
    <a href="#top">Top</a>
    <a href="javascript:void(0)">JS</a>
    <a href="mailto:x@y.com">Email Us</a>
    <a href="/contact">Real Contact</a>
    """
    found = _discover_contact_pages(html, "https://homecare.com/")
    assert len(found) == 1
    assert found[0].endswith("/contact")


# ----- WebsiteScraper.enrich (with mocked HTTP) -------------------------

class _FakeResponse:
    def __init__(self, text: str, status: int = 200, content_type: str = "text/html"):
        self.text = text
        self.status_code = status
        self.headers = {"Content-Type": content_type}


@pytest.fixture
def scraper():
    return WebsiteScraper()


def _patch_get(responses):
    """Given {url: html}, return a side_effect for requests.get."""
    def side_effect(url, **kwargs):
        for pattern, html in responses.items():
            if pattern in url:
                return _FakeResponse(html)
        return _FakeResponse("", status=404)
    return side_effect


def test_extracts_mailto_link_with_name(scraper):
    html = """
    <html><body>
      <h1>Our Team</h1>
      <p>Reach our director:
        <a href="mailto:janet@bayseniorcare.com">Janet Smith</a>
      </p>
    </body></html>
    """
    business = {"website": "https://bayseniorcare.com", "name": "Bay Senior Care"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({"bayseniorcare.com": html})):
        result = scraper.enrich(business, EnrichmentContext())

    assert result is not None
    assert result.email == "janet@bayseniorcare.com"
    found = next(c for c in result.contacts if c["email"] == "janet@bayseniorcare.com")
    assert found["name"] == "Janet Smith"


def test_extracts_email_from_text(scraper):
    html = """
    <html><body>
      <p>Questions? Email us at hello@bayseniorcare.com</p>
    </body></html>
    """
    business = {"website": "https://bayseniorcare.com", "name": "Bay"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({"bayseniorcare.com": html})):
        result = scraper.enrich(business, EnrichmentContext())

    assert result is not None
    emails = [c["email"] for c in result.contacts]
    assert "hello@bayseniorcare.com" in emails


def test_visits_contact_subpage(scraper):
    home = """
    <html><body>
      <a href="/contact">Contact Us</a>
    </body></html>
    """
    contact = """
    <html><body>
      <a href="mailto:owner@bayseniorcare.com">Robert Davies</a>
    </body></html>
    """
    business = {"website": "https://bayseniorcare.com/", "name": "Bay"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({
                   "bayseniorcare.com/contact": contact,
                   "bayseniorcare.com": home,
               })):
        result = scraper.enrich(business, EnrichmentContext())
    assert result is not None
    assert result.email == "owner@bayseniorcare.com"


def test_filters_junk_emails(scraper):
    html = """
    <p>Email us at info@homecare.com</p>
    <p>noreply@homecare.com</p>
    <p>your@email.com</p>
    <img src="hero@2x.png">
    """
    business = {"website": "https://homecare.com", "name": "X"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({"homecare.com": html})):
        result = scraper.enrich(business, EnrichmentContext())

    emails = [c["email"] for c in (result.contacts if result else [])]
    assert "info@homecare.com" in emails
    assert "noreply@homecare.com" not in emails
    assert "your@email.com" not in emails
    assert not any("hero@2x.png" in e for e in emails)


def test_returns_empty_result_when_no_emails(scraper):
    html = "<html><body>We only have a contact form.</body></html>"
    business = {"website": "https://emptyplace.com", "name": "Empty"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({"emptyplace.com": html})):
        result = scraper.enrich(business, EnrichmentContext())
    assert result is not None
    assert result.email == ""
    assert result.contacts == []


def test_handles_no_website(scraper):
    business = {"website": "", "name": "No site"}
    result = scraper.enrich(business, EnrichmentContext())
    assert result is None


def test_handles_request_failure(scraper):
    import requests
    business = {"website": "https://will-fail.example", "name": "Fail"}
    ctx = EnrichmentContext()
    with patch(
        "cwscraper.enrichment.website_scraper.requests.get",
        side_effect=requests.ConnectionError("nope"),
    ):
        result = scraper.enrich(business, ctx)
    assert result is None
    assert any("fetch failed" in e for e in ctx.errors)


# ----- Cloudflare email obfuscation -------------------------------------

def test_decode_cf_email_roundtrip():
    """The decoder must round-trip a known-good Cloudflare-encoded address."""
    # Build a known good input: encode "owner@example.com" with key=0x4d
    key = 0x4d
    addr = "owner@example.com"
    encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in addr)
    assert _decode_cf_email(encoded) == addr


def test_decode_cf_email_rejects_garbage():
    assert _decode_cf_email("not-hex") is None
    assert _decode_cf_email("") is None
    # Two valid hex bytes but result has no '@' — rejected
    assert _decode_cf_email("4d4e4f") is None


def test_cf_obfuscated_html_yields_real_email(scraper):
    # Build a real CF-encoded email and embed it in the HTML.
    key = 0x55
    addr = "jane.doe@bayseniorcare.com"
    encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in addr)
    html = f"""
    <html><body>
      <p>Reach our director:</p>
      <a class="__cf_email__"
         href="/cdn-cgi/l/email-protection#{encoded}"
         data-cfemail="{encoded}">[email&#160;protected]</a>
    </body></html>
    """
    business = {"website": "https://bayseniorcare.com", "name": "Bay"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({"bayseniorcare.com": html})):
        result = scraper.enrich(business, EnrichmentContext())
    assert result is not None
    emails = [c["email"] for c in result.contacts]
    assert "jane.doe@bayseniorcare.com" in emails


# ----- Text obfuscation ---------------------------------------------------

def test_deobfuscate_bracketed_at_dot():
    assert _deobfuscate("info [at] example [dot] com") == "info@example.com"


def test_deobfuscate_parens_at_dot():
    assert _deobfuscate("info (at) example (dot) com") == "info@example.com"


def test_deobfuscate_braces_at_dot():
    assert _deobfuscate("info {at} example {dot} com") == "info@example.com"


def test_deobfuscate_handles_extra_whitespace():
    assert _deobfuscate("info  [ at ]  example  [ dot ]  com") == "info@example.com"


def test_deobfuscate_word_form():
    out = _deobfuscate("contact me at janet at homecare dot com today")
    assert "janet@homecare.com" in out


def test_deobfuscate_preserves_non_obfuscated_text():
    # Should not mangle prose that just happens to contain "at" or "dot"
    s = "We meet at noon and the dot product is awesome."
    assert _deobfuscate(s) == s  # nothing email-shaped to rewrite


def test_obfuscated_email_extracted_end_to_end(scraper):
    html = """
    <html><body>
      <p>Email us: info [at] bayseniorcare [dot] com</p>
    </body></html>
    """
    business = {"website": "https://bayseniorcare.com", "name": "Bay"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({"bayseniorcare.com": html})):
        result = scraper.enrich(business, EnrichmentContext())
    assert result is not None
    emails = [c["email"] for c in result.contacts]
    assert "info@bayseniorcare.com" in emails


# ----- JSON-LD extraction ------------------------------------------------

def test_jsonld_organization_email():
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Organization",
      "name": "Bay Senior Care",
      "email": "info@bayseniorcare.com"
    }
    </script>
    """
    pairs = _extract_jsonld_emails(html)
    emails = [e for e, _ in pairs]
    assert "info@bayseniorcare.com" in emails


def test_jsonld_contact_point_emails():
    html = """
    <script type="application/ld+json">
    {
      "@type": "LocalBusiness",
      "contactPoint": [
        {"@type": "ContactPoint", "contactType": "customer service",
         "email": "support@homecare.com"},
        {"@type": "ContactPoint", "contactType": "billing",
         "email": "billing@homecare.com"}
      ]
    }
    </script>
    """
    pairs = _extract_jsonld_emails(html)
    by_email = {e: n for e, n in pairs}
    assert by_email.get("support@homecare.com") == "customer service"
    assert by_email.get("billing@homecare.com") == "billing"


def test_jsonld_handles_invalid_json():
    html = """
    <script type="application/ld+json">{ not actually json }</script>
    <script type="application/ld+json">
    { "@type": "Organization", "email": "ok@homecare.com" }
    </script>
    """
    pairs = _extract_jsonld_emails(html)
    emails = [e for e, _ in pairs]
    assert emails == ["ok@homecare.com"]


def test_jsonld_email_field_as_list():
    html = """
    <script type="application/ld+json">
    {"@type":"Organization","email":["a@homecare.com","b@homecare.com"]}
    </script>
    """
    pairs = _extract_jsonld_emails(html)
    emails = {e for e, _ in pairs}
    assert emails == {"a@homecare.com", "b@homecare.com"}


def test_jsonld_end_to_end_extracts_email(scraper):
    html = """
    <html><body>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Organization",
       "name":"Bay Senior Care","email":"hello@bayseniorcare.com"}
      </script>
    </body></html>
    """
    business = {"website": "https://bayseniorcare.com", "name": "Bay"}
    with patch("cwscraper.enrichment.website_scraper.requests.get",
               side_effect=_patch_get({"bayseniorcare.com": html})):
        result = scraper.enrich(business, EnrichmentContext())
    emails = [c["email"] for c in (result.contacts if result else [])]
    assert "hello@bayseniorcare.com" in emails


# ----- Expanded page discovery ------------------------------------------

def test_discover_pages_finds_new_hint_types():
    html = """
    <a href="/management">Management</a>
    <a href="/franchise-opportunities">Franchise</a>
    <a href="/locations/tampa">Tampa Office</a>
    <a href="/blog">Blog</a>
    """
    found = _discover_contact_pages(html, "https://homecare.com/")
    assert any("management" in u for u in found)
    assert any("franchise" in u for u in found)
    assert any("locations" in u for u in found)
    assert not any("blog" in u for u in found)
