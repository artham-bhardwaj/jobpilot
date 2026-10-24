"""Notifications: find events worth telling you about, write them as messages once, and deliver them.

How it stays truthful and quiet:
- Events are read from the database (jobs, drafts, application events, task and source runs). Each event has a key
  (``job:12:high``) recorded in ``notification_keys`` once it is announced, so it is never announced twice.
- The first scan after Telegram is set up records every event that already exists as "baseline" and sends nothing
  about it: you are told about what happens from then on, not flooded with history.
- Routine events are grouped into one message per scan; only high matches and recruiter replies get their own.
- A message counts as delivered only when Telegram returned its message id. Failures are retried with backoff and
  are visible on the System page; they never stop job collection or anything else.
- Counts in summaries are database queries at the time of writing. Nothing is estimated.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC as _UTC
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import (Application, EmailDraft, IntegrationState, Job, Notification, NotificationKey, SourceRun,
                             TaskRun, utcnow)
from jobpilot.models.applications import ApplicationEvent
from jobpilot.services import settings_store

log = logging.getLogger(__name__)

BASELINE_KEY = "notifications.baseline"
MAX_PER_PASS = 20
SEND_INTERVAL_SECONDS = 1.1  # Telegram allows about one message per second to a chat


# ---- small state helpers --------------------------------------------------------------------------------

def get_state(session: Session, key: str) -> dict | None:
    row = session.get(IntegrationState, key)
    return row.value if row else None


def set_state(session: Session, key: str, value: dict) -> None:
    row = session.get(IntegrationState, key)
    if row is None:
        session.add(IntegrationState(key=key, value=value))
    else:
        row.value = value
    session.flush()


def _seen(session: Session, keys: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(keys), 500):
        out |= set(session.scalars(select(NotificationKey.key).where(NotificationKey.key.in_(keys[i:i + 500]))))
    return out


def _local_midnight_utc(now_utc: datetime) -> datetime:
    """Start of today in local time, as naive UTC (how timestamps are stored)."""
    local = now_utc.replace(tzinfo=_UTC).astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(_UTC).replace(tzinfo=None)


def _local_date(now_utc: datetime) -> str:
    return now_utc.replace(tzinfo=_UTC).astimezone().strftime("%Y-%m-%d")


def _local_hour(now_utc: datetime) -> int:
    return now_utc.replace(tzinfo=_UTC).astimezone().hour


def _dash(settings: Settings, path: str) -> str:
    return f"http://{settings.host}:{settings.port}{path}"


# ---- events ---------------------------------------------------------------------------------------------

def _thresholds(session: Session) -> tuple[float, float]:
    cfg = settings_store.get(session, "notifications")
    matching = settings_store.get(session, "matching")
    relevant = cfg.get("relevant_min_score") or matching["relevance_threshold"]
    high = cfg.get("high_match_min_score") or matching.get("strong_threshold", 75)
    return float(relevant), float(high)


def _job_line(settings: Settings, j: Job) -> str:
    where = " · ".join(x for x in (j.company_name, j.location) if x)
    seen = j.last_seen_at or j.first_seen_at
    return (f"{j.relevance_score:.0f} · {j.title}" + (f" — {where}" if where else "") +
            f"\n   eligibility: {j.eligibility or 'unknown'} · listed, last seen {seen:%d %b} · {_dash(settings, f'/jobs/{j.id}')}")


def _why(j: Job) -> str:
    rel = ((j.match_details or {}).get("relevance") or {}).get("components") or []
    top = sorted(rel, key=lambda c: c.get("points", 0), reverse=True)[:3]
    return "; ".join(f"{c['name']} {c.get('points', 0):g}/{c.get('max', 0):g}" for c in top)


def collect_events(session: Session, settings: Settings) -> list[dict[str, Any]]:
    """Every current event as {key, kind, text, single}. Keys are stable; which ones are new is decided later."""
    relevant_min, high_min = _thresholds(session)
    events: list[dict[str, Any]] = []
    jobs = list(session.scalars(select(Job).where(Job.status == "active", Job.user_status != "dismissed",
                                                  Job.relevance_score.is_not(None), Job.relevance_score >= relevant_min)))
    # High-match alerts already delivered before ineligible jobs were left to the digest: never announce those twice.
    alerted = _seen(session, [f"job:{j.id}:high" for j in jobs if j.eligibility == "ineligible"])
    for j in jobs:
        ineligible = j.eligibility == "ineligible" or j.hard_conflict
        if j.relevance_score >= high_min and not ineligible:
            why = _why(j)
            head = ("🎯 High match" if j.eligibility in ("eligible", "likely_eligible")
                    else "🎯 High match — eligibility unknown: review the requirements before applying")
            text = (f"{head}\n{_job_line(settings, j)}" + (f"\n   why: {why}" if why else "") +
                    (f"\n   posting: {j.source_url}" if j.source_url else "") +
                    "\nDiscovered listing: check the posting is still open before applying.")
            events.append({"key": f"job:{j.id}:high", "kind": "high_match", "text": text, "single": True,
                           "url": j.apply_url or j.source_url, "job_id": j.id})
        elif f"job:{j.id}:high" in alerted:
            continue  # announced as a high match earlier; the digest would repeat it
        else:
            line = _job_line(settings, j)
            events.append({"key": f"job:{j.id}:relevant", "kind": "relevant_digest",
                           "text": f"⛔ ineligible · {line}" if ineligible else line, "single": False})
    for d in session.scalars(select(EmailDraft).where(EmailDraft.status == "draft", EmailDraft.kind != "gmail_test")):
        events.append({"key": f"draft:{d.id}:review", "kind": "draft_review", "single": False,
                       "text": f"#{d.id} {d.subject[:90]} — {_dash(settings, f'/drafts/{d.id}')}"})
    for d in session.scalars(select(EmailDraft).where(EmailDraft.reply_received_at.is_not(None))):
        events.append({"key": f"draft:{d.id}:reply", "kind": "reply", "single": True,
                       "text": f"📬 Reply detected on “{d.subject[:90]}”\n{_dash(settings, f'/drafts/{d.id}')}\n"
                               "Follow-ups on this thread are now suppressed."})
    from jobpilot.services import drafts as ds

    for d in ds.due_followups(session):
        events.append({"key": f"draft:{d.id}:followup:{d.follow_up_due_at:%Y%m%d}", "kind": "follow_up", "single": False,
                       "text": f"“{d.subject[:80]}” — sent {d.sent_at:%d %b}, no reply · {_dash(settings, f'/drafts/{d.id}')}"})
    from jobpilot.services import tracker

    for ev in session.scalars(select(ApplicationEvent).order_by(ApplicationEvent.id)):
        if tracker.is_email_outcome_event(ev):
            continue  # announced on its own, with an Undo button (tracker.events)
        app = ev.application
        title = app.job.title if app and app.job else f"application #{ev.application_id}"
        events.append({"key": f"appevent:{ev.id}", "kind": "application_status", "single": False,
                       "text": f"{title}: {ev.from_status or 'new'} → {ev.to_status} · "
                               f"{_dash(settings, f'/applications/{ev.application_id}')}"})
    from jobpilot.models import ReviewItem

    from jobpilot.models import ApplicationQuestion, Contact
    from jobpilot.services.applications import OPEN_STATES
    from jobpilot.services.drafts import OUTREACH_KINDS

    from jobpilot.services.answers import is_legal

    for q in session.scalars(select(ApplicationQuestion).where(ApplicationQuestion.status == "unanswered")):
        app = q.application
        if app is None or app.status not in OPEN_STATES:
            continue
        legal = is_legal(q.question)
        events.append({"key": f"question:{q.id}", "kind": "questions", "single": False, "question_id": q.id,
                       "question": q.question,
                       "text": (f"{'⚖️ legal — complete it yourself on the form, not here' if legal else f'Q{q.id}'}"
                                f"{' 🔒 personal' if q.sensitive and not legal else ''} · #{app.id} {app.job.title} at "
                                f"{app.job.company_name or '?'}: {q.question[:160]}")})
    for c in session.scalars(select(Contact).where(Contact.role_type == "recruiter")):
        events.append({"key": f"contact:{c.id}:new", "kind": "recruiter_found", "single": False,
                       "text": f"{c.display_name}{' — ' + c.title if c.title else ''} at "
                               f"{c.company.name if c.company else '?'} · email: {c.email or 'unavailable'}"
                               f"{' (' + c.email_status + ')' if c.email else ''} · source: {c.source_type} · "
                               f"{_dash(settings, f'/contacts/{c.id}')}"})
        if c.email_status == "bounced":
            events.append({"key": f"contact:{c.id}:bounced", "kind": "bounce", "single": False,
                           "text": f"{c.display_name} <{c.email}>: mail bounced; the address is now blocked for outreach"})
    for d in session.scalars(select(EmailDraft).where(EmailDraft.kind.in_(OUTREACH_KINDS),
                                                      EmailDraft.gmail_status == "created", EmailDraft.sent_at.is_(None))):
        events.append({"key": f"draft:{d.id}:gmail", "kind": "outreach_gmail", "single": False,
                       "text": f"#{d.id} to {d.to_address}: “{d.subject[:80]}” — in Gmail Drafts, NOT sent · "
                               f"{_dash(settings, f'/drafts/{d.id}')}"})
    for item in session.scalars(select(ReviewItem).where(ReviewItem.kind.in_(OUTREACH_REVIEW_KINDS),
                                                         ReviewItem.status == "open")):
        events.append({"key": f"review:{item.id}", "kind": "recruiter_needed", "single": False,
                       "text": f"{item.title}\n   ➡️ {(item.reason or '')[:300]}"})
    for d in session.scalars(select(EmailDraft).where(EmailDraft.kind.in_(OUTREACH_KINDS), EmailDraft.sent_via == "gmail",
                                                      EmailDraft.gmail_message_id.is_not(None))):
        events.append({"key": f"draft:{d.id}:sent", "kind": "outreach_sent", "single": True,
                       "text": f"📧 Recruiter email SENT (Gmail confirmed message {d.gmail_message_id})\n"
                               f"To: {d.to_address}\nSubject: {d.subject[:120]}\nJob: #{d.job_id} · "
                               f"{_dash(settings, f'/drafts/{d.id}')}\nReplies will be tracked; follow-ups are never sent "
                               "for you."})
    events += tracker.events(session, settings)
    for run in session.scalars(select(TaskRun).where(TaskRun.status == "failed")):
        events.append({"key": f"task:{run.id}:failed", "kind": "task_failure", "single": False,
                       "text": f"{run.task_name} at {run.started_at:%d %b %H:%M} UTC: {(run.message or '')[:200]}"})
    for run in session.scalars(select(SourceRun).where(SourceRun.dry_run.is_(False),
                                                       SourceRun.status.in_(("failed", "unavailable", "anomaly")))):
        name = run.source.name if getattr(run, "source", None) else f"source #{run.source_id}"
        events.append({"key": f"sourcerun:{run.id}", "kind": "source_failure", "single": False,
                       "text": f"{name}: {run.status} at {run.started_at:%d %b %H:%M} UTC — existing jobs were not changed"})
    return events


OUTREACH_REVIEW_KINDS = ("recruiter_needed", "recruiter_unverified", "outreach_review", "outreach_failed")

GROUP_TITLES = {
    "relevant_digest": "🔎 New relevant jobs",
    "draft_review": "✍️ Drafts ready for your review (nothing is sent until you approve and send)",
    "follow_up": "⏰ Follow-ups due (JobPilot never sends them for you)",
    "application_status": "📋 Application status changes",
    "task_failure": "⚠️ Task failures",
    "source_failure": "⚠️ Job source problems",
    "questions": "❓ Application questions waiting for your answer — reply /answer Q<number> <your answer>",
    "recruiter_found": "👤 Recruiter contacts added (facts as recorded; an email status other than 'verified' is "
                       "not confirmed)",
    "outreach_gmail": "✉️ Recruiter emails in Gmail Drafts — review and send them yourself; nothing was sent",
    "bounce": "↩️ Bounced recruiter emails",
    "recruiter_needed": "🔍 Recruiter outreach needs you (nothing was sent for these)",
}


def daily_summary(session: Session, settings: Settings, now: datetime | None = None) -> str:
    """Today's activity from the database (local day). Says plainly when there was nothing."""
    from jobpilot.services import drafts as ds
    from jobpilot.services.applications import SUBMITTED_STATES

    now = now or utcnow()
    start = _local_midnight_utc(now)
    relevant_min, high_min = _thresholds(session)
    new_jobs = list(session.scalars(select(Job).where(Job.first_seen_at >= start)))
    relevant = [j for j in new_jobs if (j.relevance_score or 0) >= relevant_min and j.user_status != "dismissed"]
    # strong matches: high score AND not ineligible (same rule as high-match alerts)
    high = [j for j in relevant if j.relevance_score >= high_min and not j.hard_conflict and j.eligibility != "ineligible"]
    drafts_waiting = session.scalar(select(func.count(EmailDraft.id)).where(EmailDraft.status == "draft",
                                                                            EmailDraft.kind != "gmail_test")) or 0
    approvals = session.scalar(select(func.count(Application.id)).where(Application.status == "ready_for_review")) or 0
    submitted = session.scalar(select(func.count(Application.id)).where(Application.applied_at >= start,
                                                                        Application.status.in_(SUBMITTED_STATES))) or 0
    replies = session.scalar(select(func.count(EmailDraft.id)).where(EmailDraft.reply_received_at >= start)) or 0
    followups = len(ds.due_followups(session))
    failed = list(session.scalars(select(TaskRun).where(TaskRun.status == "failed", TaskRun.started_at >= start)))
    last_collect = session.scalar(select(func.max(TaskRun.finished_at)).where(TaskRun.task_name == "collect_jobs",
                                                                               TaskRun.status == "success"))
    lines = [f"📊 JobPilot daily summary — {_local_date(now)}",
             f"Jobs discovered today: {len(new_jobs)}",
             f"New relevant (score ≥ {relevant_min:g}): {len(relevant)}" + ("" if relevant else " — none today"),
             f"Strong matches (score ≥ {high_min:g}, not ineligible): {len(high)}"]
    for j in sorted(high, key=lambda x: -x.relevance_score)[:5]:
        mark = "✅" if j.eligibility in ("eligible", "likely_eligible") else "❓ review"
        lines.append(f" • {mark} {j.relevance_score:.0f} {j.title}" + (f" — {j.company_name}" if j.company_name else ""))
    from jobpilot.models import ApplicationQuestion, Interview, ReviewItem

    questions = session.scalar(select(func.count(ApplicationQuestion.id)).where(
        ApplicationQuestion.status == "unanswered")) or 0
    emails_sent = session.scalar(select(func.count(EmailDraft.id)).where(EmailDraft.sent_via == "gmail",
                                                                         EmailDraft.gmail_message_id.is_not(None),
                                                                         EmailDraft.sent_at >= start)) or 0
    recruiter_tasks = session.scalar(select(func.count(ReviewItem.id)).where(
        ReviewItem.kind.in_(OUTREACH_REVIEW_KINDS), ReviewItem.status == "open")) or 0
    upcoming = list(session.scalars(select(Interview).where(Interview.scheduled_at >= now,
                                                            Interview.scheduled_at <= now + timedelta(days=7))
                                    .order_by(Interview.scheduled_at).limit(5)))
    lines += [f"Applications recorded as submitted today (with confirmation evidence): {submitted}",
              f"Application questions / legal items waiting for you: {questions}",
              f"Recruiter emails sent today (Gmail confirmed): {emails_sent}",
              f"Recruiter tasks for you (find / verify a contact, review an email): {recruiter_tasks}",
              f"Interviews in the next 7 days: {len(upcoming)}"]
    lines += [f" • {iv.scheduled_at:%d %b %H:%M} — {iv.application.job.title if iv.application and iv.application.job else '?'}"
              for iv in upcoming]
    lines += [f"Drafts awaiting your review: {drafts_waiting}",
              f"Applications awaiting your submission approval: {approvals}",
              f"Recruiter replies detected today: {replies}" + ("" if replies else " — none"),
              f"Follow-ups due: {followups}",
              f"Failed tasks today: {len(failed)}" + (" — " + ", ".join(sorted({f.task_name for f in failed})) if failed else ""),
              "Last successful job collection: " + (f"{last_collect:%d %b %H:%M} UTC" if last_collect else "never"),
              _dash(settings, "/")]
    return "\n".join(lines)


# ---- writing messages -----------------------------------------------------------------------------------

def _with_buttons(n: Notification, kind: str, events: list[dict]) -> Notification:
    """Buttons for a message, unless it already has its own (e.g. a submission batch's decisions)."""
    if n.reply_markup is None:
        from jobpilot.services import telegram_ui

        n.reply_markup = telegram_ui.notification_markup(kind, events)
    return n


def _add(session: Session, kind: str, text: str, keys: list[str], dedupe_key: str | None = None) -> Notification:
    dedupe = dedupe_key or (keys[0] if len(keys) == 1 else
                            f"{kind}:{hashlib.sha256('|'.join(sorted(keys)).encode()).hexdigest()[:24]}")
    n = Notification(kind=kind, dedupe_key=dedupe[:200], text=text, status="pending", refs=keys)
    session.add(n)
    session.flush()
    for k in keys:
        session.add(NotificationKey(key=k, notification_id=n.id))
    session.flush()
    return n


def scan(session: Session, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    """Turn new events into pending messages. Idempotent: running it twice creates nothing new."""
    now = now or utcnow()
    cfg = settings_store.get(session, "notifications")
    kinds = cfg.get("kinds", {})
    events = collect_events(session, settings)
    seen = _seen(session, [e["key"] for e in events])
    new = [e for e in events if e["key"] not in seen]
    created: list[Notification] = []
    if get_state(session, BASELINE_KEY) is None:
        for e in new:  # what already exists when notifications start is history, not news
            session.add(NotificationKey(key=e["key"], notification_id=None))
        set_state(session, BASELINE_KEY, {"at": now.isoformat(timespec="seconds"), "events": len(new)})
        baseline = len(new)
        new = []
    else:
        baseline = 0
    limit = int(cfg.get("max_items_per_message", 10))
    groups: dict[str, list[dict]] = {}
    for e in new:
        if not kinds.get(e["kind"], True):
            session.add(NotificationKey(key=e["key"], notification_id=None))  # turned off: never announce later
            continue
        if e["single"]:
            created.append(_with_buttons(_add(session, e["kind"], e["text"], [e["key"]]), e["kind"], [e]))
        else:
            groups.setdefault(e["kind"], []).append(e)
    for kind, items in groups.items():
        shown = items[:limit]
        text = GROUP_TITLES[kind] + f" ({len(items)})\n" + "\n".join(f"• {i['text']}" for i in shown)
        if len(items) > limit:
            text += f"\n…and {len(items) - limit} more: {_dash(settings, '/review')}"
        created.append(_with_buttons(_add(session, kind, text, [i["key"] for i in items]), kind, shown))
    # Daily summary: once per local day, at or after the configured hour.
    if kinds.get("daily_summary", True) and _local_hour(now) >= int(cfg.get("daily_summary_hour", 20)):
        key = f"daily:{_local_date(now)}"
        if not _seen(session, [key]):
            created.append(_with_buttons(_add(session, "daily_summary", daily_summary(session, settings, now), [key]),
                                         "daily_summary", []))
    session.flush()
    return {"created": len(created), "baseline": baseline, "kinds": sorted({n.kind for n in created})}


# ---- delivery -------------------------------------------------------------------------------------------

def deliver(session: Session, settings: Settings, client=None, now: datetime | None = None, sleep=None) -> dict[str, Any]:
    """Send due pending messages to Telegram. A message is ``sent`` only with Telegram's message id."""
    import time as _time

    from jobpilot.integrations import telegram

    now = now or utcnow()
    sleep = sleep or _time.sleep
    pending = list(session.scalars(select(Notification).where(
        Notification.status == "pending",
        (Notification.next_attempt_at.is_(None)) | (Notification.next_attempt_at <= now))
        .order_by(Notification.id).limit(MAX_PER_PASS)))
    if not pending:
        return {"sent": 0, "failed": 0, "deferred": 0}
    if not telegram.is_ready(settings):
        return {"sent": 0, "failed": 0, "deferred": len(pending),
                "message": "Telegram not configured: " + "; ".join(telegram.config_status(settings)["problems"])}
    client = client or telegram.TelegramClient(settings)
    max_attempts = int(settings_store.get(session, "notifications").get("max_attempts", 5))
    sent = failed = deferred = 0
    for i, n in enumerate(pending):
        if i:
            sleep(SEND_INTERVAL_SECONDS)
        n.attempts += 1
        try:
            n.external_ids = client.send_text(n.text, reply_markup=n.reply_markup, html=True)
            n.status, n.sent_at, n.last_error, n.next_attempt_at = "sent", utcnow(), None, None
            sent += 1
        except telegram.TelegramError as exc:
            n.last_error = telegram.redact(str(exc), settings)
            if exc.retryable and n.attempts < max_attempts:
                wait = exc.retry_after or 60 * 2 ** (n.attempts - 1)
                n.next_attempt_at = utcnow() + timedelta(seconds=min(wait, 6 * 3600))
                deferred += 1
            else:
                n.status = "failed"
                failed += 1
            log.warning("notification not delivered", extra={"notification": n.id, "attempt": n.attempts,
                                                              "retryable": exc.retryable})
            if not exc.retryable:  # a configuration problem: the rest would fail the same way
                session.commit()
                break
        session.commit()  # each message's outcome is recorded as soon as it is known
    return {"sent": sent, "failed": failed, "deferred": deferred,
            "message": f"{sent} sent, {deferred} to retry, {failed} failed"}


def run(session: Session, settings: Settings, client=None) -> dict[str, Any]:
    """Scheduled task: scan for new events and deliver. Does nothing (and says so) if Telegram is not set up."""
    from jobpilot.integrations import telegram

    status = telegram.config_status(settings)
    if not status["ready"]:
        return {"message": "Telegram not configured; skipped (" + "; ".join(status["problems"]) + ")"}
    if not settings_store.get(session, "notifications").get("enabled", True):
        return {"message": "Notifications turned off in settings; skipped"}
    found = scan(session, settings)
    session.commit()
    res = deliver(session, settings, client)
    msg = f"{found['created']} new message(s)"
    if found["baseline"]:
        msg += f"; {found['baseline']} existing event(s) recorded as baseline (not announced)"
    return {**res, "message": f"{msg}; {res.get('message', 'nothing to send')}", "created": found["created"]}


def recent(session: Session, limit: int = 20) -> list[Notification]:
    return list(session.scalars(select(Notification).order_by(Notification.id.desc()).limit(limit)))


def counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Notification.status, func.count(Notification.id)).group_by(Notification.status))
    return {s: n for s, n in rows}
