"""Send-volume hygiene: daily cap, per-domain throttle, warm-up curve.

These exist for one reason: **deliverability**. Sending 970 cold emails from
hello@checkwellcall.co on day one will get the mailbox throttled by
Hostinger, spam-foldered by Gmail/Outlook, and burn the sender reputation
of the domain. Reputation takes weeks to recover; an hour of bulk sending
can make the next two months of legitimate outreach worthless.

Three independent guardrails:

1. **Daily cap** — never send more than N emails in a UTC day, counting both
   sent and pending-queued. Default 50.

2. **Per-domain cap** — never send more than N emails to the same recipient
   domain in one UTC day. Prevents Gmail from seeing 20 cold emails from
   the same sender to 20 of its addresses in one hour. Default 5.

3. **Warm-up curve** — for the first 14 days after the first send, the
   effective daily cap is staged: 10/day → 25/day → 50/day. Skipped
   entirely if you set ``CWSCRAPER_SEND_WARMUP_DISABLED=true``.

All limits read from env vars at call time, so changing a Railway variable
takes effect on the next dispatcher tick / bulk-draft request without a
restart.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cwscraper.email.send_limits")


# ---------------------------------------------------------------------------
# Env-driven settings (read at every call so updates are live)
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def settings_summary() -> dict:
    """For the dashboard / preflight banner. Includes effective caps for
    today (warm-up curve applied)."""
    base_daily = _env_int("CWSCRAPER_SEND_DAILY_CAP", 50)
    per_domain = _env_int("CWSCRAPER_SEND_PER_DOMAIN_DAILY_CAP", 5)
    warmup_disabled = _env_bool("CWSCRAPER_SEND_WARMUP_DISABLED", False)
    return {
        "daily_cap_base": base_daily,
        "daily_cap_effective": effective_daily_cap(base_daily, warmup_disabled),
        "per_domain_daily_cap": per_domain,
        "warmup_disabled": warmup_disabled,
        "warmup_day": _warmup_day(),
        "first_send_date": _first_send_date_iso(),
    }


# ---------------------------------------------------------------------------
# Warm-up curve
# ---------------------------------------------------------------------------

# Stored at ``<data_dir>/first_send_date.txt`` as an ISO date. Written the
# first time the daily-cap check runs after the queue has at least one sent
# email — pinpoints the warm-up start.

def _data_dir() -> Path:
    raw = os.getenv("CWSCRAPER_DATA_DIR", "./data")
    path = Path(raw).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _first_send_date_file() -> Path:
    return _data_dir() / "first_send_date.txt"


def _first_send_date_iso() -> str:
    p = _first_send_date_file()
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def record_first_send_date_if_unset() -> None:
    """Called by the dispatcher after the first successful send. Stamps
    today (UTC) into the warm-up start file. Idempotent."""
    p = _first_send_date_file()
    if p.exists():
        return
    try:
        p.write_text(datetime.now(timezone.utc).date().isoformat(), encoding="utf-8")
    except OSError as e:
        logger.warning("could not persist first_send_date: %s", e)


def _warmup_day() -> int:
    """Days elapsed since first send (0 if never sent, capped at 99)."""
    iso = _first_send_date_iso()
    if not iso:
        return 0
    try:
        first = datetime.fromisoformat(iso).date()
    except ValueError:
        return 0
    today = datetime.now(timezone.utc).date()
    return min((today - first).days, 99)


def effective_daily_cap(base_cap: int | None = None, warmup_disabled: bool | None = None) -> int:
    """Compute today's effective daily cap with the warm-up curve applied.

    Days 0–6:  10 (or base, whichever is lower)
    Days 7–13: 25 (or base, whichever is lower)
    Day 14+:   base
    """
    if base_cap is None:
        base_cap = _env_int("CWSCRAPER_SEND_DAILY_CAP", 50)
    if warmup_disabled is None:
        warmup_disabled = _env_bool("CWSCRAPER_SEND_WARMUP_DISABLED", False)
    if warmup_disabled or not _first_send_date_iso():
        return base_cap
    day = _warmup_day()
    if day < 7:
        return min(10, base_cap)
    if day < 14:
        return min(25, base_cap)
    return base_cap


# ---------------------------------------------------------------------------
# Counting today's sends + per-domain
# ---------------------------------------------------------------------------

def _today_utc_iso_prefix() -> str:
    """YYYY-MM-DD for today (UTC). Used as a string-prefix match against
    queue entries' ``scheduled_for`` / ``sent_at`` fields."""
    return datetime.now(timezone.utc).date().isoformat()


def _domain_of(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].strip().lower()


def todays_counts(queue) -> dict[str, int]:
    """Count today's queue entries by domain + total. Counts both sent and
    pending statuses — pending is committed volume from the model's
    perspective."""
    today = _today_utc_iso_prefix()
    by_domain: dict[str, int] = {}
    total = 0
    # `queue.list()` is paginated but lightweight — pull all pending+sent
    pending = queue.list(status="pending") or []
    sent = queue.list(status="sent") or []
    for entry in pending + sent:
        # We bucket by the queue entry's scheduled_for (intended day). For
        # sent entries, sent_at and scheduled_for are very close.
        scheduled = (entry.get("scheduled_for") or "")[:10]
        if scheduled != today:
            continue
        total += 1
        d = _domain_of(entry.get("to_email") or "")
        if d:
            by_domain[d] = by_domain.get(d, 0) + 1
    return {"total": total, "by_domain": by_domain}


# ---------------------------------------------------------------------------
# Public check used by the dispatcher and bulk drafter
# ---------------------------------------------------------------------------

def check_can_queue(to_email: str, queue) -> tuple[bool, str]:
    """Return (allowed, reason). ``reason`` is empty when allowed."""
    if not to_email or "@" not in to_email:
        return False, "missing-or-invalid-email"

    counts = todays_counts(queue)
    cap = effective_daily_cap()
    if counts["total"] >= cap:
        return False, f"daily-cap ({counts['total']}/{cap} used today)"

    per_domain = _env_int("CWSCRAPER_SEND_PER_DOMAIN_DAILY_CAP", 5)
    d = _domain_of(to_email)
    if d and counts["by_domain"].get(d, 0) >= per_domain:
        return False, f"per-domain-cap (@{d}: {counts['by_domain'][d]}/{per_domain} today)"

    return True, ""
