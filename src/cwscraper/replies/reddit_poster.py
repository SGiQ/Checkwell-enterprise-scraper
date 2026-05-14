"""Reddit OAuth flow + posting. Tokens are stored in the Repository's config."""
from __future__ import annotations

import logging
import os
import time

import requests

from cwscraper.core.store import Repository
from cwscraper.scanners.reddit import REDDIT_HEADERS

logger = logging.getLogger("cwscraper.reddit_oauth")


class RedditOAuth:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.client_id = os.getenv("REDDIT_CLIENT_ID", "")
        self.client_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
        self.redirect_uri = os.getenv(
            "CWSCRAPER_REDDIT_REDIRECT_URI", "http://localhost:5050/auth/reddit/callback"
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorize_url(self, state: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": self.redirect_uri,
            "duration": "permanent",
            "scope": "submit identity",
        }
        return f"https://www.reddit.com/api/v1/authorize?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict | None:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(self.client_id, self.client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            headers={"User-Agent": REDDIT_HEADERS["User-Agent"]},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Reddit token exchange failed: %s", resp.text[:200])
            return None
        data = resp.json()
        config = self.repo.get_config()
        config["reddit_access_token"] = data.get("access_token", "")
        config["reddit_refresh_token"] = data.get("refresh_token", "")
        config["reddit_token_expires"] = time.time() + data.get("expires_in", 3600)
        self.repo.save_config(config)

        # Look up username for display.
        me = requests.get(
            "https://oauth.reddit.com/api/v1/me",
            headers={
                "Authorization": f"Bearer {data['access_token']}",
                "User-Agent": REDDIT_HEADERS["User-Agent"],
            },
            timeout=15,
        )
        if me.status_code == 200:
            config["reddit_username"] = me.json().get("name", "")
            self.repo.save_config(config)
        return data

    def current_token(self) -> str | None:
        config = self.repo.get_config()
        token = config.get("reddit_access_token")
        if not token:
            return None
        if time.time() < config.get("reddit_token_expires", 0) - 60:
            return token

        # Refresh
        refresh = config.get("reddit_refresh_token")
        if not refresh or not self.configured:
            return None
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            headers={"User-Agent": REDDIT_HEADERS["User-Agent"]},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        config["reddit_access_token"] = data.get("access_token", "")
        config["reddit_token_expires"] = time.time() + data.get("expires_in", 3600)
        self.repo.save_config(config)
        return config["reddit_access_token"]

    def disconnect(self) -> None:
        config = self.repo.get_config()
        for key in (
            "reddit_access_token",
            "reddit_refresh_token",
            "reddit_token_expires",
            "reddit_username",
        ):
            config.pop(key, None)
        self.repo.save_config(config)


def post_reddit_comment(oauth: RedditOAuth, post_url: str, text: str) -> dict:
    token = oauth.current_token()
    if not token:
        return {"error": "Not authenticated with Reddit. Connect in Settings."}

    parts = post_url.rstrip("/").split("/")
    post_id = None
    for i, part in enumerate(parts):
        if part == "comments" and i + 1 < len(parts):
            post_id = parts[i + 1]
            break
    if not post_id:
        return {"error": f"Could not extract post ID from URL: {post_url}"}

    resp = requests.post(
        "https://oauth.reddit.com/api/comment",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": REDDIT_HEADERS["User-Agent"],
        },
        data={"thing_id": f"t3_{post_id}", "text": text},
        timeout=15,
    )
    if resp.status_code != 200:
        return {"error": f"Reddit API returned {resp.status_code}: {resp.text[:200]}"}
    body = resp.json()
    errors = body.get("json", {}).get("errors", [])
    if errors:
        return {"error": f"Reddit API errors: {errors}"}
    return {"success": True}
