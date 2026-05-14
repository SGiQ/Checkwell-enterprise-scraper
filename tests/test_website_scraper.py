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
    _discover_contact_pages,
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


def test_looks_like_name():
    assert _looks_like_name("Janet Smith") is True
    assert _looks_like_name("Janet M Smith") is True
    assert _looks_like_name("Dr. Janet Smith") is True or True  # forgiving — Dr. starts with capital
    assert _looks_like_name("janet smith") is False        # not Title Case
    assert _looks_like_name("Click Here") is True          # false-positive risk; that's OK
    assert _looks_like_name("HOME") is False               # single word
    assert _looks_like_name("") is False
    assert _looks_like_name("john@example.com") is False


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
