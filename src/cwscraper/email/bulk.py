"""Bulk personalized outreach drafter — queue many emails in one action.

The operator picks N businesses in the dashboard, picks a template, and
hits "Draft + queue". This module loops over the selection, runs each
business through the personalizer (if the template uses
``{personalized_opener}``), drafts the email, enforces the send-hygiene
limits (suppression, daily cap, per-domain cap), and enqueues each one
with staggered ``scheduled_for`` times so the dispatcher doesn't fire 25
emails in one minute.

Synchronous on purpose: even at 25 businesses × ~700ms personalization,
this takes ~20 seconds and the operator can wait. A future iteration can
move it to a background thread with progress streaming if needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from cwscraper.email.send_limits import check_can_queue
from cwscraper.replies.outreach import draft_outreach

logger = logging.getLogger("cwscraper.email.bulk")

DEFAULT_CADENCE_SECONDS = 180   # 3 minutes between sends in the batch
MIN_CADENCE_SECONDS = 30        # below this, deliverability suffers
MAX_BATCH_SIZE = 200             # one bulk action shouldn't exceed this


def bulk_draft_and_queue(
    *,
    business_ids: list[str],
    repo,
    queue,
    suppression,
    niche,
    personalizer=None,
    template_key: str | None = None,
    start_at: datetime | None = None,
    cadence_seconds: int = DEFAULT_CADENCE_SECONDS,
    from_email: str = "",
    from_name: str = "",
    reply_to: str = "",
    progress_cb=None,
) -> dict:
    """Draft + enqueue N personalized cold emails in one action.

    Args:
        business_ids: Business row IDs to process, in operator-selected order.
        repo: JSONRepository — used to read business rows.
        queue: ScheduledEmailQueue — receives the enqueued emails.
        suppression: SuppressionList — skipped recipients are recorded
            with ``status='skipped_suppressed'`` in the returned results.
        niche: Active NichePack — drives template selection.
        personalizer: Optional :class:`Personalizer`. If the chosen
            template uses ``{personalized_opener}``, this drives the AI
            opener; if it isn't provided, the token is replaced by an
            empty string and the rest of the template still renders.
        template_key: Which template to use (defaults to
            ``niche.default_outreach_template``).
        start_at: First send time (UTC). Defaults to "now + 60 seconds"
            so the operator has a moment to cancel if they hit Draft by
            mistake.
        cadence_seconds: Seconds between consecutive sends in the batch.
            Clamped to >= ``MIN_CADENCE_SECONDS``.
        from_email/from_name/reply_to: Passed through to the queue
            entry. Defaults are picked up from env via the transport at
            send time.
        progress_cb: Optional ``progress_cb(i, n, result)`` called after
            each business — used by progress UI.

    Returns:
        Summary dict with per-business outcomes and aggregate counts.
    """
    if not business_ids:
        return _empty_summary("no business IDs provided")

    if len(business_ids) > MAX_BATCH_SIZE:
        return _empty_summary(
            f"batch too large: {len(business_ids)} (max {MAX_BATCH_SIZE})"
        )

    cadence = max(MIN_CADENCE_SECONDS, int(cadence_seconds or DEFAULT_CADENCE_SECONDS))
    if start_at is None:
        start_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    elif start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)

    # Index businesses by ID for one-pass lookup (avoids N repo reads)
    businesses_by_id = {b.get("id"): b for b in repo.get_businesses()}

    n = len(business_ids)
    results: list[dict] = []
    counts = {
        "received": n,
        "queued": 0,
        "skipped_not_found": 0,
        "skipped_no_email": 0,
        "skipped_suppressed": 0,
        "skipped_hygiene": 0,    # daily cap or per-domain cap hit
        "skipped_template_error": 0,
        "personalized": 0,        # AI opener generated
        "personalization_fallback": 0,  # API failed, used regional opener
    }
    next_send_at = start_at

    for idx, biz_id in enumerate(business_ids):
        biz = businesses_by_id.get(biz_id)
        if not biz:
            results.append({"business_id": biz_id, "status": "skipped_not_found"})
            counts["skipped_not_found"] += 1
            _emit_progress(progress_cb, idx + 1, n, results[-1])
            continue

        to_email = (biz.get("email") or "").strip()
        if not to_email:
            # Try first contact's email
            contacts = biz.get("contacts") or []
            for c in contacts:
                if c.get("email"):
                    to_email = c["email"].strip()
                    break
        if not to_email:
            results.append({"business_id": biz_id, "status": "skipped_no_email"})
            counts["skipped_no_email"] += 1
            _emit_progress(progress_cb, idx + 1, n, results[-1])
            continue

        # Suppression check
        if suppression.is_suppressed(to_email):
            results.append({
                "business_id": biz_id, "to_email": to_email,
                "status": "skipped_suppressed",
            })
            counts["skipped_suppressed"] += 1
            _emit_progress(progress_cb, idx + 1, n, results[-1])
            continue

        # Daily cap + per-domain cap
        allowed, reason = check_can_queue(to_email, queue)
        if not allowed:
            results.append({
                "business_id": biz_id, "to_email": to_email,
                "status": "skipped_hygiene", "reason": reason,
            })
            counts["skipped_hygiene"] += 1
            _emit_progress(progress_cb, idx + 1, n, results[-1])
            # Stop early if the daily cap is hit — none of the remaining
            # selections can be queued today either.
            if reason.startswith("daily-cap"):
                break
            continue

        # Render the draft (personalizer is consulted inside draft_outreach
        # only when the template uses {personalized_opener})
        try:
            draft = draft_outreach(
                biz, niche, template_key=template_key,
                personalizer=personalizer,
            )
        except Exception as e:
            logger.exception("draft_outreach failed for %s", biz_id)
            results.append({
                "business_id": biz_id, "to_email": to_email,
                "status": "skipped_template_error", "error": str(e),
            })
            counts["skipped_template_error"] += 1
            _emit_progress(progress_cb, idx + 1, n, results[-1])
            continue

        p_info = draft.get("personalization")
        if p_info:
            if p_info.get("error"):
                counts["personalization_fallback"] += 1
            else:
                counts["personalized"] += 1

        # Enqueue with staggered scheduled_for
        scheduled_for = next_send_at.isoformat()
        entry = queue.enqueue(
            prospect_id=biz_id,
            lead_type="business",
            to_email=to_email,
            subject=draft["subject"],
            body=draft["body"],
            scheduled_for=scheduled_for,
            from_email=from_email,
            from_name=from_name,
            reply_to=reply_to,
        )
        counts["queued"] += 1
        results.append({
            "business_id": biz_id,
            "to_email": to_email,
            "queue_id": entry["id"],
            "scheduled_for": scheduled_for,
            "subject": draft["subject"],
            "personalized": bool(draft.get("personalized_opener")),
            "status": "queued",
        })
        _emit_progress(progress_cb, idx + 1, n, results[-1])

        next_send_at = next_send_at + timedelta(seconds=cadence)

    return {
        "ok": True,
        "template_key": template_key or niche.default_outreach_template,
        "start_at": start_at.isoformat(),
        "cadence_seconds": cadence,
        **counts,
        "results": results,
    }


def _empty_summary(reason: str) -> dict:
    return {
        "ok": False,
        "error": reason,
        "received": 0, "queued": 0,
        "skipped_not_found": 0, "skipped_no_email": 0,
        "skipped_suppressed": 0, "skipped_hygiene": 0,
        "skipped_template_error": 0,
        "personalized": 0, "personalization_fallback": 0,
        "results": [],
    }


def _emit_progress(progress_cb, i, n, result):
    if progress_cb is None:
        return
    try:
        progress_cb(i, n, result)
    except Exception:
        logger.exception("progress_cb raised — continuing batch")
