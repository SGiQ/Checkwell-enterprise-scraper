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
