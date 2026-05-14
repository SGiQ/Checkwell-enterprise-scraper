from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from cwscraper.core.models import Lead
from cwscraper.core.scoring import classify_intent
from cwscraper.scanners.base import BaseScanner, ScannerContext, logger

REDDIT_HEADERS = {
    "User-Agent": "CWScraper:LeadAgent:v0.1 (community research)",
}
REQUEST_DELAY = 1.5


class RedditScanner(BaseScanner):
    platform = "reddit"

    def scan(self, ctx: ScannerContext) -> tuple[list[Lead], dict[str, float]]:
        all_leads: list[Lead] = []
        all_new_seen: dict[str, float] = {}

        for sub in self.niche.enabled_subreddits():
            logger.info("Scanning r/%s", sub)
            leads, seen = self._scan_subreddit(sub, ctx)
            all_leads.extend(leads)
            all_new_seen.update(seen)
            ctx.seen_ids.update(seen.keys())

        return all_leads, all_new_seen

    def _scan_subreddit(
        self, subreddit: str, ctx: ScannerContext
    ) -> tuple[list[Lead], dict[str, float]]:
        leads: list[Lead] = []
        new_seen: dict[str, float] = {}

        data = self._get(
            f"https://www.reddit.com/r/{subreddit}/new.json",
            params={"limit": 50},
            ctx=ctx,
        )
        if not data or "data" not in data:
            return leads, new_seen

        for post in data["data"].get("children", []):
            pd = post.get("data", {})
            post_id = pd.get("id", "")
            if not post_id or post_id in ctx.seen_ids:
                continue

            new_seen[post_id] = datetime.now(timezone.utc).timestamp()
            combined = f"{pd.get('title', '')} {pd.get('selftext', '')}"
            intent, matched = classify_intent(
                combined,
                self.niche.high_intent_keywords,
                self.niche.medium_intent_keywords,
            )
            if not intent:
                continue

            raw_text = pd.get("selftext", "")
            leads.append(
                Lead(
                    id=post_id,
                    platform="reddit",
                    source=f"r/{subreddit}",
                    title=pd.get("title", ""),
                    url=f"https://reddit.com{pd.get('permalink', '')}",
                    score=pd.get("score", 0),
                    num_comments=pd.get("num_comments", 0),
                    created_utc=pd.get("created_utc", 0),
                    selftext_preview=raw_text[:500],
                    intent_level=intent,
                    matched_keywords=matched,
                )
            )

        return leads, new_seen

    def _get(self, url: str, params: dict | None, ctx: ScannerContext) -> dict | None:
        time.sleep(REQUEST_DELAY)
        try:
            resp = requests.get(url, headers=REDDIT_HEADERS, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Reddit rate limited, waiting 60s")
                time.sleep(60)
                return self._get(url, params, ctx)
            ctx.log_error("Reddit", f"HTTP {resp.status_code} for {url}")
            return None
        except requests.RequestException as e:
            ctx.log_error("Reddit", f"Request error: {e}")
            return None

    def discover_subreddits(self, queries: list[str], ctx: ScannerContext) -> list[dict]:
        """Find candidate subreddits for the niche."""
        found: dict[str, dict] = {}
        for query in queries:
            data = self._get(
                "https://www.reddit.com/search.json",
                params={"q": query, "sort": "relevance", "limit": 10},
                ctx=ctx,
            )
            if not data or "data" not in data:
                continue
            for post in data["data"].get("children", []):
                sub = post.get("data", {}).get("subreddit", "")
                if sub and sub.lower() not in found:
                    info = self._get(f"https://www.reddit.com/r/{sub}/about.json", None, ctx)
                    if info and "data" in info:
                        d = info["data"]
                        found[sub.lower()] = {
                            "name": d.get("display_name", sub),
                            "subscribers": d.get("subscribers", 0),
                            "active_users": d.get("accounts_active", 0),
                            "description": d.get("public_description", "")[:200],
                            "discovered_via": query,
                        }
        return sorted(found.values(), key=lambda x: x["subscribers"], reverse=True)
