from __future__ import annotations

from typing import Iterable


def classify_intent(
    text: str,
    high_intent_keywords: Iterable[str],
    medium_intent_keywords: Iterable[str],
) -> tuple[str | None, list[str]]:
    """Classify lead intent from text.

    Returns (intent_level, matched_keywords) where intent_level is
    'high', 'medium', or None if no keywords matched.
    """
    haystack = text.lower()
    high = [kw for kw in high_intent_keywords if kw in haystack]
    if high:
        return "high", high
    medium = [kw for kw in medium_intent_keywords if kw in haystack]
    if medium:
        return "medium", medium
    return None, []
