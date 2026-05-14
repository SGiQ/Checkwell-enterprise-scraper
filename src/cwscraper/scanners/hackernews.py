from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

from cwscraper.core.models import Lead
from cwscraper.core.scoring import classify_intent
from cwscraper.scanners.base import BaseScanner, ScannerContext
from cwscraper.scanners.reddit import REQUEST_DELAY


class HackerNewsScanner(BaseScanner):
    """HN Algolia full-text search — no auth, fast, reliable."""

    platform = "hackernews"

    def scan(self, ctx: ScannerContext) -> tuple[list[Lead], dict[str, float]]:
        leads: list[Lead] = []
        new_seen: dict[str, float] = {}

        for query in self.niche.hackernews_queries:
            time.sleep(REQUEST_DELAY)
            try:
                resp = requests.get(
                    "https://hn.algolia.com/api/v1/search_by_date",
                    params={"query": query, "tags": "(story,comment)", "hitsPerPage": 20},
                    timeout=15,
                )
                if resp.status_code != 200:
                    continue

                for hit in resp.json().get("hits", []):
                    hn_id = str(hit.get("objectID", ""))
                    lid = f"hn_{hn_id}"
                    if lid in ctx.seen_ids:
                        continue

                    title = hit.get("title") or (hit.get("comment_text", "") or "")[:100]
                    text = hit.get("comment_text") or hit.get("story_text") or ""
                    text = re.sub(r"<[^>]+>", " ", text)

                    intent, matched = classify_intent(
                        f"{title} {text}",
                        self.niche.high_intent_keywords,
                        self.niche.medium_intent_keywords,
                    )
                    if not intent:
                        continue

                    new_seen[lid] = datetime.now(timezone.utc).timestamp()
                    story_id = hit.get("story_id") or hn_id
                    leads.append(
                        Lead(
                            id=lid,
                            platform="hackernews",
                            source="HackerNews",
                            title=hit.get("title") or text[:80],
                            url=f"https://news.ycombinator.com/item?id={story_id}",
                            score=hit.get("points") or 0,
                            num_comments=hit.get("num_comments") or 0,
                            selftext_preview=text[:500],
                            intent_level=intent,
                            matched_keywords=matched,
                        )
                    )
            except requests.RequestException as e:
                ctx.log_error("HackerNews", f"Search error: {e}")

        return leads, new_seen
