"""Google Places API (New) — text search for businesses by category + location.

Uses the modern Places API endpoint (places.googleapis.com/v1/places:searchText)
which gives field-mask cost control and replaces the legacy Places API.

Pricing reference (as of writing):
  - Text Search (Pro fields only): ~$35 per 1k requests after $200/mo free credit
  - With FieldMask trimmed to discovery essentials, each request stays in the
    cheapest SKU tier.

Requires env var GOOGLE_PLACES_API_KEY.
"""
from __future__ import annotations

import os
import re
import time

import requests

from cwscraper.core.models import BusinessLead
from cwscraper.scanners.directory_base import BaseDirectoryScanner, DirectoryContext, logger

# Comma-separated FieldMask — REQUIRED by the new API.
# Adding fields here can move billing into a more expensive SKU tier, so keep
# this list to "discovery essentials" only. Enrichment adds nothing here.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.location",
    "places.regularOpeningHours.weekdayDescriptions",
    "places.types",
])

PLACES_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
REQUEST_DELAY = 0.5  # respect Google's QPS limits


class GooglePlacesScanner(BaseDirectoryScanner):
    source = "google_places"

    def scan(self, ctx: DirectoryContext) -> list[BusinessLead]:
        api_key = os.getenv("GOOGLE_PLACES_API_KEY", "")
        if not api_key:
            ctx.log_error(self.source, "GOOGLE_PLACES_API_KEY not set — skipping")
            return []

        cfg = self.niche.directory
        if not cfg.search_queries or not cfg.locations:
            ctx.log_error(self.source, "niche pack has no search_queries or locations")
            return []

        results: list[BusinessLead] = []
        seen_place_ids: set[str] = set()

        for location in cfg.locations:
            for query in cfg.search_queries:
                text_query = f"{query} in {location}"
                hits = self._search_text(api_key, text_query, cfg.max_per_query, ctx)
                ctx.queries_run += 1
                for raw in hits:
                    biz = _parse_place(raw, discovered_via=text_query, niche=self.niche)
                    if not biz or biz.id in seen_place_ids:
                        continue
                    if cfg.min_rating and biz.rating and biz.rating < cfg.min_rating:
                        continue
                    seen_place_ids.add(biz.id)
                    results.append(biz)

        logger.info("Google Places: %d unique businesses across %d queries",
                    len(results), ctx.queries_run)
        return results

    def _search_text(
        self, api_key: str, text_query: str, max_results: int, ctx: DirectoryContext
    ) -> list[dict]:
        time.sleep(REQUEST_DELAY)
        try:
            resp = requests.post(
                PLACES_ENDPOINT,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": FIELD_MASK,
                },
                json={
                    "textQuery": text_query,
                    "maxResultCount": min(max(max_results, 1), 20),
                },
                timeout=20,
            )
        except requests.RequestException as e:
            ctx.log_error(self.source, f"Request error for '{text_query}': {e}")
            return []

        if resp.status_code == 403:
            ctx.log_error(
                self.source,
                "API key rejected (403). Enable 'Places API (New)' in your "
                "Google Cloud project and confirm the key isn't restricted to a different API."
            )
            return []
        if resp.status_code == 429:
            ctx.log_error(self.source, f"Rate limited (429) on '{text_query}' — backing off")
            time.sleep(5)
            return []
        if resp.status_code != 200:
            ctx.log_error(
                self.source,
                f"HTTP {resp.status_code} for '{text_query}': {resp.text[:200]}"
            )
            return []

        return resp.json().get("places", [])


# --- response parser -------------------------------------------------------

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _parse_place(raw: dict, discovered_via: str, niche) -> BusinessLead | None:
    """Map a single /places:searchText result into a BusinessLead."""
    place_id = raw.get("id") or ""
    if not place_id:
        return None

    name_block = raw.get("displayName") or {}
    name = name_block.get("text", "") if isinstance(name_block, dict) else str(name_block)
    if not name:
        return None

    address = raw.get("formattedAddress", "")
    city, state, zip_code = _extract_address_parts(raw.get("addressComponents", []), address)

    location = raw.get("location") or {}
    hours_block = raw.get("regularOpeningHours") or {}
    hours_lines = hours_block.get("weekdayDescriptions") or []

    return BusinessLead(
        id=place_id,
        source="google_places",
        name=name,
        category=niche.directory.category_label or _first_business_type(raw.get("types", [])),
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
        phone=raw.get("nationalPhoneNumber", ""),
        website=raw.get("websiteUri", ""),
        rating=float(raw.get("rating", 0) or 0),
        review_count=int(raw.get("userRatingCount", 0) or 0),
        hours=" | ".join(hours_lines)[:500],
        latitude=float(location.get("latitude", 0) or 0),
        longitude=float(location.get("longitude", 0) or 0),
        discovered_via=discovered_via,
    )


def _extract_address_parts(components: list[dict], formatted: str) -> tuple[str, str, str]:
    """Pull city/state/zip from addressComponents; fall back to regex on formatted."""
    city = state = zip_code = ""
    for comp in components or []:
        types = comp.get("types", [])
        long_name = comp.get("longText") or comp.get("long_name") or ""
        short_name = comp.get("shortText") or comp.get("short_name") or ""
        if "locality" in types and not city:
            city = long_name
        elif "administrative_area_level_1" in types and not state:
            state = short_name or long_name
        elif "postal_code" in types and not zip_code:
            zip_code = long_name

    if not zip_code:
        m = _ZIP_RE.search(formatted)
        if m:
            zip_code = m.group(1)
    return city, state, zip_code


def _first_business_type(types: list[str]) -> str:
    skip = {"point_of_interest", "establishment", "premise"}
    for t in types or []:
        if t not in skip:
            return t
    return types[0] if types else ""
