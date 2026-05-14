from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

import requests

from cwscraper.core.models import Lead
from cwscraper.core.scoring import classify_intent
from cwscraper.scanners.base import BaseScanner, ScannerContext, logger
from cwscraper.scanners.reddit import REDDIT_HEADERS, REQUEST_DELAY


class YouTubeScanner(BaseScanner):
    """Uses YouTube Data API v3 when YOUTUBE_API_KEY is set; falls back to RSS."""

    platform = "youtube"

    def scan(self, ctx: ScannerContext) -> tuple[list[Lead], dict[str, float]]:
        api_key = os.getenv("YOUTUBE_API_KEY", "")
        if api_key:
            logger.info("YouTube: using API key")
            return self._scan_via_api(api_key, ctx)
        logger.info("YouTube: no API key, falling back to RSS")
        return self._scan_via_rss(ctx)

    def _scan_via_api(
        self, api_key: str, ctx: ScannerContext
    ) -> tuple[list[Lead], dict[str, float]]:
        leads: list[Lead] = []
        new_seen: dict[str, float] = {}

        for query in self.niche.youtube_queries:
            time.sleep(REQUEST_DELAY)
            try:
                resp = requests.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={
                        "part": "snippet",
                        "q": query,
                        "type": "video",
                        "maxResults": 10,
                        "order": "relevance",
                        "key": api_key,
                    },
                    timeout=15,
                )
                if resp.status_code == 403:
                    ctx.log_error("YouTube", "API key rejected (403) — check quota or validity")
                    break
                if resp.status_code != 200:
                    ctx.log_error("YouTube", f"API returned {resp.status_code} for '{query}'")
                    continue

                data = resp.json()
                if "error" in data:
                    ctx.log_error("YouTube", f"API error: {data['error'].get('message', '')[:100]}")
                    break

                for item in data.get("items", []):
                    video_id = item.get("id", {}).get("videoId", "")
                    lid = f"yt_{video_id}"
                    if not video_id or lid in ctx.seen_ids:
                        continue

                    snippet = item.get("snippet", {})
                    title = snippet.get("title", "")
                    desc = snippet.get("description", "")
                    intent, matched = classify_intent(
                        f"{title} {desc}",
                        self.niche.high_intent_keywords,
                        self.niche.medium_intent_keywords,
                    )
                    # Accept every search hit — the query itself was already targeted.
                    if not intent:
                        intent = "medium"
                        matched = [query]

                    new_seen[lid] = datetime.now(timezone.utc).timestamp()
                    leads.append(
                        Lead(
                            id=lid,
                            platform="youtube",
                            source=snippet.get("channelTitle", "YouTube"),
                            title=title,
                            url=f"https://www.youtube.com/watch?v={video_id}",
                            selftext_preview=desc[:500],
                            intent_level=intent,
                            matched_keywords=matched,
                        )
                    )

                    # Pull high-intent comments from the video too.
                    for cl in self._video_comments(api_key, video_id, ctx):
                        if cl.id not in ctx.seen_ids:
                            new_seen[cl.id] = datetime.now(timezone.utc).timestamp()
                            leads.append(cl)

            except requests.RequestException as e:
                ctx.log_error("YouTube", f"API request error: {e}")

        return leads, new_seen

    def _video_comments(
        self, api_key: str, video_id: str, ctx: ScannerContext
    ) -> list[Lead]:
        leads: list[Lead] = []
        time.sleep(REQUEST_DELAY)
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={
                    "part": "snippet",
                    "videoId": video_id,
                    "maxResults": 50,
                    "order": "relevance",
                    "key": api_key,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return leads
            for item in resp.json().get("items", []):
                comment = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                text = comment.get("textDisplay", "")
                comment_id = item.get("id", "")
                lid = f"ytc_{comment_id}"
                if not comment_id or lid in ctx.seen_ids:
                    continue
                intent, matched = classify_intent(
                    text, self.niche.high_intent_keywords, self.niche.medium_intent_keywords
                )
                if not intent:
                    continue
                leads.append(
                    Lead(
                        id=lid,
                        platform="youtube",
                        source="YouTube Comment",
                        title=text[:100],
                        url=f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
                        score=comment.get("likeCount", 0),
                        selftext_preview=text[:500],
                        intent_level=intent,
                        matched_keywords=matched,
                    )
                )
        except requests.RequestException as e:
            ctx.log_error("YouTube", f"Comments error: {e}")
        return leads

    def _scan_via_rss(self, ctx: ScannerContext) -> tuple[list[Lead], dict[str, float]]:
        leads: list[Lead] = []
        new_seen: dict[str, float] = {}

        for channel in self.niche.youtube_channels:
            channel_name = channel.get("name", "")
            channel_id = channel.get("id", "")
            if not channel_id:
                continue
            time.sleep(REQUEST_DELAY)
            try:
                resp = requests.get(
                    f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                    headers={"User-Agent": REDDIT_HEADERS["User-Agent"]},
                    timeout=15,
                )
                if resp.status_code != 200:
                    continue

                entries = re.findall(r"<entry>(.*?)</entry>", resp.text, re.DOTALL)
                for entry in entries[:10]:
                    vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", entry)
                    title_m = re.search(r"<title>(.*?)</title>", entry)
                    if not vid or not title_m:
                        continue
                    video_id = vid.group(1)
                    title = title_m.group(1)
                    lid = f"yt_{video_id}"
                    if lid in ctx.seen_ids:
                        continue
                    intent, matched = classify_intent(
                        title, self.niche.high_intent_keywords, self.niche.medium_intent_keywords
                    )
                    if not intent:
                        continue
                    new_seen[lid] = datetime.now(timezone.utc).timestamp()
                    leads.append(
                        Lead(
                            id=lid,
                            platform="youtube",
                            source=channel_name,
                            title=title,
                            url=f"https://www.youtube.com/watch?v={video_id}",
                            intent_level=intent,
                            matched_keywords=matched,
                        )
                    )
            except requests.RequestException as e:
                ctx.log_error("YouTube", f"RSS error: {e}")

        return leads, new_seen
