"""Pre-flight configuration check.

Answers the question "is this install actually ready to run a scan?" by
introspecting env vars + active niche pack. Surfaces:
  - blockers  — things that will cause the scan to return zero results
  - warnings  — things that will degrade results but won't break the scan
  - notes     — informational, optional improvements

Used by the dashboard to show a readiness banner before the user wastes
a click. Also useful for CI / cron-job sanity checks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from cwscraper.core.niche import NichePack


@dataclass
class PreflightCheck:
    blockers: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    env_status: dict[str, bool] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return len(self.blockers) == 0

    @property
    def status(self) -> str:
        if self.blockers:
            return "blocked"
        if self.warnings:
            return "warning"
        return "ready"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "ready": self.ready,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "notes": self.notes,
            "env_status": self.env_status,
        }


def _env_present(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def evaluate(niche: NichePack) -> PreflightCheck:
    """Check readiness for the active niche pack."""
    pf = PreflightCheck()

    # Always-relevant env var inventory (helps the user see what's set)
    pf.env_status = {
        "GOOGLE_PLACES_API_KEY": _env_present("GOOGLE_PLACES_API_KEY"),
        "YOUTUBE_API_KEY":       _env_present("YOUTUBE_API_KEY"),
        "REDDIT_CLIENT_ID":      _env_present("REDDIT_CLIENT_ID"),
        "REDDIT_CLIENT_SECRET":  _env_present("REDDIT_CLIENT_SECRET"),
        "CWSCRAPER_SECRET":      _env_present("CWSCRAPER_SECRET"),
        "CWSCRAPER_DATA_DIR":    _env_present("CWSCRAPER_DATA_DIR"),
        "RESEND_API_KEY":        _env_present("RESEND_API_KEY"),
        "SMTP_HOST":             _env_present("SMTP_HOST"),
        "SMTP_USER":             _env_present("SMTP_USER"),
        "SMTP_PASSWORD":         _env_present("SMTP_PASSWORD"),
        "CWSCRAPER_FROM_EMAIL":  _env_present("CWSCRAPER_FROM_EMAIL"),
        "CWSCRAPER_FROM_NAME":   _env_present("CWSCRAPER_FROM_NAME"),
    }

    # Email scheduling is optional but should warn on half-config.
    has_resend = pf.env_status["RESEND_API_KEY"]
    has_smtp = all(pf.env_status[k] for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
    has_partial_smtp = (
        any(pf.env_status[k] for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
        and not has_smtp
    )
    has_from = pf.env_status["CWSCRAPER_FROM_EMAIL"]
    has_any_transport = has_resend or has_smtp

    if has_partial_smtp:
        pf.warnings.append({
            "code": "smtp_partial_config",
            "title": "SMTP transport partially configured",
            "detail": (
                "SMTP needs all of SMTP_HOST, SMTP_USER, and SMTP_PASSWORD. "
                "For Gmail/Workspace: smtp.gmail.com + your email + a 16-char "
                "App Password (https://myaccount.google.com/apppasswords)."
            ),
        })
    if has_resend and not has_from:
        pf.warnings.append({
            "code": "resend_partial_config",
            "title": "Resend transport partially configured",
            "detail": (
                "RESEND_API_KEY is set but CWSCRAPER_FROM_EMAIL is not. "
                "Set the sender address (verified domain in Resend)."
            ),
        })
    if not has_any_transport:
        pf.notes.append({
            "code": "email_scheduler_not_configured",
            "title": "Email scheduler not configured",
            "detail": (
                "Pick one to enable scheduled sends: "
                "(a) Gmail SMTP — set SMTP_HOST=smtp.gmail.com SMTP_PORT=587 "
                "SMTP_USER=you@your-domain.com SMTP_PASSWORD=<16-char App Password>. "
                "(b) Resend — sign up at resend.com (3k/mo free) and set "
                "RESEND_API_KEY + CWSCRAPER_FROM_EMAIL. Until one's set, "
                "'Draft Email' opens your default mail client only."
            ),
        })

    if niche.mode == "directory":
        _evaluate_directory(niche, pf)
    else:
        _evaluate_community(niche, pf)

    # Cross-cutting note: secret key on default
    if not pf.env_status["CWSCRAPER_SECRET"]:
        pf.notes.append({
            "code": "default_secret",
            "title": "Flask secret is still the default",
            "detail": (
                "CWSCRAPER_SECRET is unset — sessions use a development default. "
                "Set this to a long random string in production."
            ),
        })

    return pf


def _evaluate_directory(niche: NichePack, pf: PreflightCheck) -> None:
    # Google Places API key is required for directory mode (the only directory
    # scanner today). Without it, scans return zero results.
    if not pf.env_status["GOOGLE_PLACES_API_KEY"]:
        pf.blockers.append({
            "code": "no_google_places_key",
            "title": "GOOGLE_PLACES_API_KEY is not set",
            "detail": (
                "Directory-mode niches scan Google Places — without this key the scan "
                "will return zero results. Add it in Railway -> Variables, then redeploy."
            ),
            "action": "set_env",
            "env_var": "GOOGLE_PLACES_API_KEY",
        })

    # Niche pack sanity: must have at least one search query and one location.
    cfg = niche.directory
    if not cfg.search_queries:
        pf.blockers.append({
            "code": "no_search_queries",
            "title": "Niche pack has no search queries",
            "detail": (
                f"'{niche.slug}' is directory-mode but defines no search_queries. "
                "Add them under the 'directory:' block in the YAML pack."
            ),
        })
    if not cfg.locations:
        pf.blockers.append({
            "code": "no_locations",
            "title": "Niche pack has no locations",
            "detail": (
                f"'{niche.slug}' is directory-mode but defines no locations. "
                "Add them under the 'directory:' block in the YAML pack."
            ),
        })

    # Outreach templates are optional but useful
    if not niche.outreach_templates:
        pf.warnings.append({
            "code": "no_outreach_templates",
            "title": "No cold-email templates configured",
            "detail": (
                "Directory-mode pack has no outreach_templates. Draft Email "
                "buttons will fall back to an empty draft."
            ),
        })


def _evaluate_community(niche: NichePack, pf: PreflightCheck) -> None:
    # Reddit is the bedrock community source, but Reddit now blocks the
    # unauthenticated .json endpoints with HTTP 403 (from cloud *and* most
    # local IPs). The scanner falls back to app-only OAuth when the client
    # creds are set — so without them every subreddit 403s and returns 0.
    if not (pf.env_status["REDDIT_CLIENT_ID"] and pf.env_status["REDDIT_CLIENT_SECRET"]):
        pf.warnings.append({
            "code": "no_reddit_oauth",
            "title": "Reddit API credentials not configured",
            "detail": (
                "Reddit blocks unauthenticated .json requests with HTTP 403, so "
                "subreddit scans will return zero results. Set REDDIT_CLIENT_ID + "
                "REDDIT_CLIENT_SECRET (create a 'web app' at "
                "reddit.com/prefs/apps) — that alone enables app-only scanning. "
                "Connecting an account via Settings is only needed to POST replies."
            ),
            "action": "set_env",
            "env_vars": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
        })

    # YouTube API key is optional — RSS fallback works without it
    if not pf.env_status["YOUTUBE_API_KEY"]:
        pf.notes.append({
            "code": "no_youtube_api_key",
            "title": "YouTube API key not set",
            "detail": (
                "Without YOUTUBE_API_KEY, YouTube scanner falls back to RSS feeds "
                "of channels listed in the niche pack. Set the key for broader coverage."
            ),
        })

    # Niche pack sanity
    if not niche.high_intent_keywords and not niche.medium_intent_keywords:
        pf.blockers.append({
            "code": "no_keywords",
            "title": "Niche pack has no keywords",
            "detail": (
                f"'{niche.slug}' is community-mode but defines no intent keywords. "
                "Posts will never match. Add high_intent_keywords / medium_intent_keywords "
                "to the YAML pack."
            ),
        })
    if not niche.subreddits:
        pf.warnings.append({
            "code": "no_subreddits",
            "title": "No Reddit communities configured",
            "detail": (
                f"'{niche.slug}' has no subreddits listed. Reddit scanning will "
                "be skipped entirely; only YouTube + HN will run."
            ),
        })
