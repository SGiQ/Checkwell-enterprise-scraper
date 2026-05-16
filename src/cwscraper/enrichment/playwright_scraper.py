"""Headless-browser enricher — cracks JS-rendered sites where mailto:
and contact info only appear after the page's scripts run.

Inherits all email-extraction logic from WebsiteScraper (Cloudflare decoder,
JSON-LD walker, text-obfuscation handler, etc.). Overrides only `_fetch` to
swap `requests.get` for a real Chromium render.

Heavy: each Chromium instance is ~200 MB RAM and a page render is 2-5 s.
For ~500 businesses, a full sweep is 30-60 minutes and uses real Railway memory.

Optional install. If Playwright/Chromium aren't installed:
    pip install '.[playwright]' && playwright install chromium
The Dockerfile in this repo already does both at image-build time.
"""
from __future__ import annotations

import time
from typing import Optional

from cwscraper.enrichment.base import EnrichmentContext, EnrichmentResult
from cwscraper.enrichment.website_scraper import (
    MAX_PAGES_PER_BUSINESS,
    USER_AGENT,
    WebsiteScraper,
    _discover_contact_pages,
)

PAGE_LOAD_TIMEOUT_MS = 20_000     # hard cap on initial navigation
NETWORK_IDLE_TIMEOUT_MS = 5_000   # wait for JS to settle after load
PER_PAGE_DELAY = 0.3              # small politeness gap between pages


class PlaywrightScraper(WebsiteScraper):
    """Same extraction logic as WebsiteScraper, but rendered through Chromium."""

    source = "playwright"

    # Lower concurrency than the plain scraper — each worker spawns a Chromium.
    # The engine reads this to choose pool size.
    suggested_workers = 2

    def __init__(self):
        super().__init__()
        self._sync_playwright = None
        self._available = False
        try:
            from playwright.sync_api import sync_playwright
            self._sync_playwright = sync_playwright
            self._available = True
        except ImportError:
            pass

        # These live for the duration of one enrich() call.
        self._pw = None
        self._browser = None
        self._page = None

    @property
    def available(self) -> bool:
        return self._available

    def enrich(self, business: dict, ctx: EnrichmentContext) -> Optional[EnrichmentResult]:
        if not self._available:
            ctx.log_error(
                business.get("name", "?"),
                "playwright not installed — pip install '.[playwright]' "
                "&& playwright install chromium"
            )
            return None

        url = (business.get("website") or "").strip()
        if not url:
            return None

        # Browser lifecycle scoped to one business.
        self._pw = self._sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = self._browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            self._page = context.new_page()
            try:
                return super().enrich(business, ctx)
            finally:
                self._page.close()
                context.close()
        finally:
            if self._browser:
                self._browser.close()
                self._browser = None
            if self._pw:
                self._pw.stop()
                self._pw = None
            self._page = None

    def _fetch(self, url, business, ctx):
        """Replaces the requests-based fetch with a Playwright page render."""
        if not self._page:
            ctx.log_error(business.get("name", url), "playwright page not initialized")
            return None

        time.sleep(PER_PAGE_DELAY)
        try:
            response = self._page.goto(
                url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS,
            )
        except Exception as e:
            ctx.log_error(
                business.get("name", url),
                f"playwright goto failed [{url}]: {_brief_error(e)}",
            )
            return None

        # If the server returned a hard error status, log it so we can tell
        # cloudflare blocks (403/503) from timeouts in the dashboard errors panel.
        if response is not None and response.status >= 400:
            ctx.log_error(
                business.get("name", url),
                f"playwright HTTP {response.status} [{url}]",
            )
            # 4xx still gives us HTML sometimes (custom error pages with contact info);
            # 5xx and bot-block pages usually don't. Try to read content anyway —
            # the extraction passes will yield nothing if there's nothing to parse.

        # Best-effort wait for JS-driven content to finish loading.
        try:
            self._page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except Exception:
            # networkidle is a hint — sites with long-polling/analytics often
            # never go idle. Proceed with whatever rendered so far.
            pass

        try:
            html = self._page.content()
        except Exception as e:
            ctx.log_error(
                business.get("name", url),
                f"playwright content() failed [{url}]: {_brief_error(e)}",
            )
            return None

        # Same memory cap as the requests-based version
        return html[:500_000]


def _brief_error(exc: Exception) -> str:
    """Compact one-line summary of a Playwright exception.

    Playwright raises a single generic Error class for everything, so
    type(exc).__name__ is uninformative. Strip the message of stack-trace
    noise (Playwright errors are multi-line with call-log dumps) and take
    just the first line so dashboard error toasts stay readable.
    """
    msg = (str(exc) or repr(exc)).strip()
    first_line = msg.split("\n", 1)[0].strip()
    # Playwright timeouts read "Page.goto: Timeout 20000ms exceeded." — keep that
    # but cap length so 800-line stack traces don't overwhelm the UI.
    return first_line[:160] or type(exc).__name__


def discover_pages_via_browser(html: str, base_url: str) -> list[str]:
    """Convenience re-export so callers don't need to reach into the base module."""
    return _discover_contact_pages(html, base_url)


# Make the page cap visible to callers tuning expectations.
DEFAULT_MAX_PAGES = MAX_PAGES_PER_BUSINESS
