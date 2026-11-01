"""Manager agent: runs the Search, Resume and Apply agents as one cycle and decides what each works on.

One cycle (scheduler task ``agents``, every few hours, or ``jobpilot agents run``, or "Run now" on the Agents page):

1. Recover: a submission interrupted by a crash becomes "uncertain" (never repeated).
2. **Search agent**: new jobs from every source, feeds, alert emails and newly found company boards.
3. Pick: the best-scoring active jobs that fit your policy (``Settings → agents``: score, eligibility, job families,
   age), that you haven't applied to, aren't waiting on you, and aren't at a company in its cooldown. Dream companies
   are prepared for you instead of applied to (``hold_dream_companies``). At most ``max_per_cycle``, and never more
   than what's left of ``daily_limit`` today.
4. **Resume agent**: a validated tailored resume for each picked job.
5. **Apply agent**: email or form where safe; everything else goes to your "Needs you" list.
6. Report: what each agent did is saved for the Agents page, and a short Telegram summary is sent.

Nothing is sent or submitted in ``dry_run`` mode, or while paused (Telegram /pause).
"""

from __future__ import annotations

import copy
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.agents import apply as apply_agent
from jobpilot.agents import resume as resume_agent
from jobpilot.agents import search as search_agent
from jobpilot.agents.base import Report, applied_today, is_paused, job_fits, policy, save_report
from jobpilot.config import Settings
from jobpilot.models import Application, ApplicationQuestion, Job, ReviewItem, utcnow
from jobpilot.services import notifications, review
from jobpilot.services.text_utils import normalize_company

log = logging.getLogger(__name__)

DRY_SEEN = "agents.dry_run_seen"
DRY_REPEAT_HOURS = 24


def _cooldown_reason(session: Session, job: Job) -> str | None:
    from jobpilot.services import tracker

    days = tracker.cooldown_days(session, job.company_name)
    if not days:
        return None
    key = normalize_company(job.company_name)
    since = utcnow() - timedelta(days=days)
    for app in session.scalars(select(Application).where(Application.applied_at >= since)):
        if normalize_company(app.job.company_name) == key:
            return f"{job.company_name} cooldown: applied {app.applied_at:%d %b} (#{app.id}), wait {days} days"
    return None


def _retry_ready(session: Session, item: ReviewItem) -> bool:
    """A form that stopped on unanswered questions can continue once you've answered all of them."""
    if (item.data or {}).get("code") != "form":
        return False
    open_q = session.scalar(select(ApplicationQuestion.id).where(ApplicationQuestion.application_id == item.entity_id,
                                                                 ApplicationQuestion.status == "unanswered").limit(1))
    asked = session.scalar(select(ApplicationQuestion.id).where(ApplicationQuestion.application_id == item.entity_id)
                           .limit(1))
    return asked is not None and open_q is None


def select_jobs(session: Session, pol: dict[str, Any], mode: str) -> dict[str, Any]:
    """{"picked": [...], "held": [...], "excluded": {reason: count}}. Changes nothing except closing "Needs you"
    items whose questions you have since answered (those jobs are picked again)."""
    from jobpilot.services import tracker

    seen = (notifications.get_state(session, DRY_SEEN) or {}) if mode != "auto" else {}
    recent = (utcnow() - timedelta(hours=DRY_REPEAT_HOURS)).isoformat()
    picked, held, excluded = [], [], {}

    def skip(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    rows = session.scalars(select(Job).where(Job.status == "active").order_by(Job.relevance_score.desc().nulls_last(),
                                                                              Job.id))
    for job in rows:
        if job_fits(job, pol):
            skip("outside the policy")
            continue
        app = apply_agent.apps.for_job(session, job.id)
        if app is not None:
            if app.applied_at is not None or app.status in ("withdrawn", "rejected", "applied"):
                skip("already applied or closed")
                continue
            item = apply_agent.open_item(session, app.id)
            if item is not None:
                if _retry_ready(session, item):
                    review.resolve_items(session, apply_agent.NEEDS_YOU, "application", app.id,
                                         "questions answered; the Apply agent continues")
                else:
                    skip("waiting on you")
                    continue
            if any(r.status in apply_agent.NEVER_RETRY for r in app.browser_runs):
                skip("submitted or uncertain earlier")
                continue
        draft = apply_agent._email_in_flight(session, job)  # noqa: SLF001
        if draft is not None and (draft.status == "sent" or draft.gmail_status in ("sending", "send_unknown", "sent")):
            skip("application email already sent")
            continue
        if _cooldown_reason(session, job):
            skip("company cooldown")
            continue
        if seen.get(str(job.id), "") > recent:
            skip("checked in a dry run in the last 24 h")
            continue
        if pol["hold_dream_companies"] and tracker.is_dream(session, job.company_name):
            if app is None and len(held) < int(pol["max_per_cycle"]):
                held.append(job)
            else:
                skip("dream company (kept for you)")
            continue
        if len(picked) < int(pol["max_per_cycle"]):
            picked.append(job)
        else:
            skip("over this cycle's limit")
    return {"picked": picked, "held": held, "excluded": excluded}


def run_cycle(session: Session, settings: Settings, *, only: str | None = None, mode: str | None = None, http=None,
              gmail_client=None, runner=None, rehearse=None) -> dict[str, Any]:
    """One full cycle (or one agent with ``only`` = search | resume | apply). Returns a summary for the task log."""
    pol = policy(session)
    mode = mode or pol["apply_mode"]
    manager = Report("manager")
    reports: list[Report] = []
    recovered = apply_agent.recover(session)
    if recovered:
        manager.add("recovered", recovered)
    session.commit()

    if only in (None, "search"):
        rep = search_agent.run(session, settings, pol, http=http, gmail_client=gmail_client,
                               force_collect=only == "search")
        reports.append(rep)
        save_report(session, rep)
        session.commit()
    if only == "search":
        return _finish(session, settings, pol, mode, manager, reports, {"picked": [], "held": [], "excluded": {}})

    if mode == "auto" and not is_paused(session):
        room = max(0, int(pol["daily_limit"]) - applied_today(session))
        pol = {**pol, "max_per_cycle": min(int(pol["max_per_cycle"]), room)} if room < int(pol["max_per_cycle"]) else pol
    chosen = select_jobs(session, pol, mode)
    session.commit()
    rep, ready = resume_agent.run(session, settings, pol, chosen["picked"] + chosen["held"])
    reports.append(rep)
    save_report(session, rep)
    session.commit()
    if only in (None, "apply"):
        rep = apply_agent.run(session, settings, pol, mode, chosen["picked"], ready, chosen["held"],
                              gmail_client=gmail_client, runner=runner, rehearse=rehearse)
        reports.append(rep)
        save_report(session, rep)
        if mode != "auto":
            state = copy.deepcopy(notifications.get_state(session, DRY_SEEN) or {})
            now = utcnow().isoformat(timespec="seconds")
            state.update({str(j.id): now for j in chosen["picked"]})
            cutoff = (utcnow() - timedelta(days=7)).isoformat()
            notifications.set_state(session, DRY_SEEN, {k: v for k, v in state.items() if v > cutoff})
        session.commit()
    return _finish(session, settings, pol, mode, manager, reports, chosen)


def _finish(session: Session, settings: Settings, pol: dict[str, Any], mode: str, manager: Report,
            reports: list[Report], chosen: dict[str, Any]) -> dict[str, Any]:
    manager.counts.update({"picked": len(chosen["picked"]), "held": len(chosen["held"])})
    manager.items = [{"outcome": "excluded", "detail": f"{n} job(s): {why}"} for why, n in
                     sorted(chosen["excluded"].items(), key=lambda kv: -kv[1])]
    parts = [f"{r.agent}: {r.summary}" for r in reports if r.summary]
    manager.summary = (("DRY RUN · " if mode != "auto" else "") + (f"picked {len(chosen['picked'])} job(s)"
                       + (f", {len(chosen['held'])} kept for you" if chosen["held"] else "")) + (" · paused" if is_paused(session) else ""))
    save_report(session, manager)
    _digest(session, settings, pol, mode, reports)
    session.commit()
    message = manager.summary + (" — " + " | ".join(parts) if parts else "")
    return {"message": message[:1000], "mode": mode, "reports": {r.agent: r.counts for r in reports},
            "picked": [j.id for j in chosen["picked"]], "held": [j.id for j in chosen["held"]]}


def _digest(session: Session, settings: Settings, pol: dict[str, Any], mode: str, reports: list[Report]) -> None:
    """A short Telegram message when the cycle did something you should know about."""
    if not pol["digest"]:
        return
    from jobpilot.services import settings_store

    kinds = settings_store.get(session, "notifications").get("kinds", {})
    if not kinds.get("agents_digest", True):
        return
    by = {r.agent: r for r in reports}
    applied = [i for r in reports for i in r.items if i["outcome"] in ("applied_email", "applied_form")]
    needs = [i for r in reports for i in r.items if i["outcome"] == "needs_you"]
    ready = [i for r in reports for i in r.items if i["outcome"] in ("would_email", "would_submit")]
    new = (by.get("search").counts.get("new_jobs", 0) if by.get("search") else 0)
    if not (applied or needs or ready or new):
        return
    url = f"http://{settings.host}:{settings.port}/agents"
    lines = [f"🤖 Agents{' (dry run)' if mode != 'auto' else ''}: {new} new job(s)"]
    for i in applied[:8]:
        lines.append(f"✅ Applied: {i.get('title')} — {i.get('company') or '?'} ({i['detail'][:80]})")
    for i in ready[:8]:
        lines.append(f"🧪 Would apply: {i.get('title')} — {i.get('company') or '?'}")
    if needs:
        lines.append(f"👉 {len(needs)} need you (one step each):")
        for i in needs[:6]:
            lines.append(f"• {i.get('title')} — {i.get('company') or '?'}: {i['detail'][:140]}")
    lines.append(url)
    stamp = utcnow().strftime("%Y%m%d%H%M%S%f")
    notifications._add(session, "agents_digest", "\n".join(lines)[:3800], [f"agents_digest:{stamp}"])  # noqa: SLF001
