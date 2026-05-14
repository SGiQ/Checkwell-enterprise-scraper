from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Lead:
    id: str = ""
    platform: str = "reddit"
    source: str = ""
    title: str = ""
    url: str = ""
    score: int = 0
    num_comments: int = 0
    created_utc: float = 0.0
    selftext_preview: str = ""
    intent_level: str = "medium"
    matched_keywords: list[str] = field(default_factory=list)
    discovered_at: str = field(default_factory=_utcnow)
    status: str = "new"


@dataclass
class BusinessLead:
    """A B2B prospect — typically a senior-care agency / facility / provider.

    Populated by directory-mode scanners (Google Places, Yelp).
    Enrichment scanners (website, Hunter, Apollo) fill in `email` and
    `contacts` in a second pass.
    """

    id: str = ""                          # place_id / yelp business_id
    source: str = "google_places"         # google_places, yelp
    name: str = ""
    category: str = ""                    # e.g. "home_care_agency"
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = "US"
    phone: str = ""
    website: str = ""
    rating: float = 0.0
    review_count: int = 0
    hours: str = ""
    latitude: float = 0.0
    longitude: float = 0.0

    # --- enrichment (filled by enrichment scanners; empty until then) ---
    email: str = ""
    contacts: list[dict] = field(default_factory=list)  # [{name, title, email, phone}]

    # --- discovery metadata ---
    discovered_via: str = ""              # search query that surfaced this business
    discovered_at: str = field(default_factory=_utcnow)
    status: str = "new"                   # new, qualified, contacted, dismissed


@dataclass
class ScanResult:
    timestamp: str = field(default_factory=_utcnow)
    duration_seconds: float = 0.0
    sources_scanned: int = 0
    posts_checked: int = 0
    leads_found: int = 0
    high_intent: int = 0
    medium_intent: int = 0
    by_source: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
