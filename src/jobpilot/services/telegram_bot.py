"""Telegram commands and buttons, answered by long polling (no webhook, no public address).

- Only messages and button presses from the configured chat id (and, for button presses, that same user) are
  handled. Anything else is recorded as ``rejected`` and gets no reply, so a stranger learns nothing.
- Commands read the database, except /answer (your own answer to one application question), /pause and /resume
  (automatic recruiter outreach) and your tracker answers. JobPilot never applies to jobs for you. Nothing here
  runs shell commands or sends email. Day-to-day work happens in the dashboard; Telegram is for alerts.
- Each update id is recorded before it is answered, and the polling offset is persisted, so an update is never
  handled twice (after a crash it may go unanswered, never answered twice).
- One poller at a time: polling holds the ``telegram_poll`` task lock (an OS file lock), so a second
  ``jobpilot serve`` or ``jobpilot telegram poll`` waits instead of competing (Telegram would reject it with 409).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import Settings
from jobpilot.integrations import telegram
from jobpilot.models import Application, EmailDraft, Job, TaskRun, TelegramUpdate
from jobpilot.services import notifications, settings_store
from jobpilot.services.tasks import TaskLocked, task_lock

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram.offset"
POLL_TIMEOUT = 25

HELP = ("JobPilot\n"
        "Type a normal message to ask Claude (when the assistant is running on your computer); /new starts over\n"
        "/usage — your Claude plan limits and what the assistant has used\n"
        "/status — system health, last discovery, schedules\n"
        "/today — today's summary\n"
        "/jobs — top active relevant jobs\n"
        "/pending — what is waiting for you\n"
        "/applications — applications by status\n"
        "/outcomes — recent browser-assisted attempts and application status changes\n"
        "/questions — application questions waiting for your answer\n"
        "/recruiters — recruiter contacts, their companies, jobs and email status\n"
        "/outreach — recruiter emails and their real state (draft / in Gmail Drafts / sent / replied)\n"
        "/outreach_pending — recruiter emails waiting for your review\n"
        "/outreach_stats — contacts, drafts, sent, replies, bounces, suppressed\n"
        "/alljobs — every relevant job with its apply link (⭐ dream companies first)\n"
        "/track — did you apply? (buttons: applied → which resume, not yet, not interested)\n"
        "/applied — your applications by company, with dates, resume and cooldown\n"
        "/company amazon — applications and open jobs at one company\n"
        "/cooldown amazon 90 — set a company's cooldown (days between applications)\n"
        "/dream — jobs at your dream companies (FAANG)\n"
        "/answer Q12 your answer — answer question 12 (add ' +bank' to reuse a non-sensitive answer later)\n"
        "/pause, /resume — stop or restart automatic recruiter outreach\n"
        "/menu — buttons for everything\n"
        "/help — this list\n"
        "All commands are read-only except /answer, /cooldown, /pause, /resume and your "
        "applied / not interested / email answers. JobPilot never applies for you.")


def _dash(settings: Settings, path: str = "/") -> str:
    return f"http://{settings.host}:{settings.port}{path}"


def cmd_status(session: Session, settings: Settings) -> str:
    from jobpilot.integrations import gmail
    from jobpilot.services import scheduler

    last_collect = session.scalar(select(func.max(TaskRun.finished_at)).where(TaskRun.task_name == "collect_jobs",
                                                                               TaskRun.status == "success"))
    failures = list(session.scalars(select(TaskRun).where(TaskRun.status == "failed").order_by(TaskRun.id.desc()).limit(3)))
    nxt = {k: v for k, v in scheduler.next_runs(session).items() if v is not None}
    active = session.scalar(select(func.count(Job.id)).where(Job.status == "active")) or 0
    lines = ["🩺 JobPilot status",
             f"Scheduler: {'running inside the server' if settings.scheduler_enabled else 'not running in the server (manual or systemd timer)'}",
             "Enabled tasks: " + (", ".join(sorted(nxt)) if nxt else "none"),
             "Last successful job collection: " + (f"{last_collect:%d %b %H:%M} UTC" if last_collect else "never"),
             f"Active jobs: {active}",
             f"Gmail: {'ready' if gmail.readiness(settings)['ready'] else 'not ready'}",
             "Notifications: " + (", ".join(f"{n} {k.replace('_', ' ')}"
                                            for k, n in sorted(notifications.counts(session).items())) or "none yet")]
    if failures:
        lines.append("Recent failures:")
        lines += [f" • {f.task_name} {f.started_at:%d %b %H:%M}: {(f.message or '')[:120]}" for f in failures]
    else:
        lines.append("No failed tasks recorded.")
    return "\n".join(lines)


_ELIG = {"eligible": "✅", "likely_eligible": "✅", "unknown": "❓", "ineligible": "⛔"}


def cmd_jobs(session: Session, settings: Settings) -> str:
    threshold = settings_store.get(session, "matching")["relevance_threshold"]
    jobs = list(session.scalars(select(Job).where(Job.status == "active", Job.user_status != "dismissed",
                                                  Job.relevance_score >= threshold)
                                .order_by(Job.relevance_score.desc()).limit(10)))
    if not jobs:
        return f"No active jobs with score ≥ {threshold:g}. {_dash(settings, '/jobs')}"
    from jobpilot.services import applications as apps, regions

    def tags(j) -> str:
        out = [", ".join(regions.LABELS[r] for r in regions.of(j))]
        if j.hard_conflict:
            out.append("hard conflict")
        app = apps.for_job(session, j.id)
        if app is not None:
            out.append(f"application #{app.id}: {app.status.replace('_', ' ')}")
        return f" ({'; '.join(out)})" if out else ""

    return (f"🔎 Top relevant jobs (score ≥ {threshold:g}; ✅ eligible / likely, ❓ needs review, ⛔ ineligible)\n" + "\n".join(
        f"• {_ELIG.get(j.eligibility, '❓')} {j.relevance_score:.0f} {j.title}" + (f" — {j.company_name}" if j.company_name else "")
        + tags(j) + f"\n   apply: {j.apply_url or j.source_url or '—'} · {_dash(settings, f'/jobs/{j.id}')}" for j in jobs)
        + "\nA high score is relevance, not eligibility: ⛔ jobs state requirements you don't meet. "
          "Nothing has been applied for unless an application says 'applied'.")


def cmd_pending(session: Session, settings: Settings) -> str:
    from jobpilot.services import drafts as ds, queue

    drafts = list(session.scalars(select(EmailDraft).where(EmailDraft.status == "draft", EmailDraft.kind != "gmail_test")))
    approved = list(session.scalars(select(EmailDraft).where(EmailDraft.status == "approved", EmailDraft.kind != "gmail_test")))
    ready = list(session.scalars(select(Application).where(Application.status == "ready_for_review")))
    lines = ["📝 Waiting for you",
             f"Drafts to review: {len(drafts)}", *[f" • #{d.id} {d.subject[:70]}" for d in drafts[:5]],
             f"Approved drafts not sent: {len(approved)}", *[f" • #{d.id} {d.subject[:70]}" for d in approved[:5]],
             f"Applications ready for your review: {len(ready)}", *[f" • {a.job.title}" for a in ready[:5] if a.job],
             f"Follow-ups due: {len(ds.due_followups(session))}",
             f"Review queue items: {len(queue.build(session))} — {_dash(settings, '/review')}"]
    return "\n".join(lines)


def cmd_applications(session: Session, settings: Settings) -> str:
    rows = list(session.execute(select(Application.status, func.count(Application.id)).group_by(Application.status)))
    if not rows:
        return f"No applications recorded yet. {_dash(settings, '/applications')}"
    latest = list(session.scalars(select(Application).order_by(Application.updated_at.desc()).limit(8)))
    return ("📋 Applications by status\n" + "\n".join(f"• {s.replace('_', ' ')}: {n}" for s, n in rows) +
            "\nLatest:\n" + "\n".join(f"• {a.job.title if a.job else '#' + str(a.id)} — {a.status.replace('_', ' ')}"
                                      for a in latest) +
            "\n'applied' means you recorded confirmation evidence; nothing is assumed.")


def cmd_pause(session: Session, settings: Settings) -> str:
    from jobpilot.services import outreach_auto

    outreach_auto.set_paused(session, True, "telegram")
    return "⏸ Automatic recruiter outreach is paused. Job collection, matching and everything else keep running. /resume"


def cmd_resume(session: Session, settings: Settings) -> str:
    from jobpilot.services import outreach_auto

    outreach_auto.set_paused(session, False, "telegram")
    on = settings_store.get(session, "outreach")["auto_enabled"]
    return "▶️ Automatic recruiter outreach resumed." + ("" if on else " It is turned off in Settings, so nothing runs.")


_RUN_LABEL = {"confirmed": "✅ confirmed (confirmation page captured)", "submit_failed": "❌ rejected by the form",
              "uncertain": "❓ uncertain — never resubmitted; check email/portal", "submitting": "❓ interrupted",
              "paused": "⏸ stopped before submitting", "failed": "⚠️ not submitted (error)"}


def cmd_outcomes(session: Session, settings: Settings) -> str:
    from jobpilot.models import ApplicationEvent, BrowserRun

    runs = list(session.scalars(select(BrowserRun).where(BrowserRun.status.in_(list(_RUN_LABEL)))
                                .order_by(BrowserRun.id.desc()).limit(8)))
    events = list(session.scalars(select(ApplicationEvent).order_by(ApplicationEvent.id.desc()).limit(8)))
    if not runs and not events:
        return f"No submission attempts or application updates yet. {_dash(settings, '/applications')}"
    lines = []
    if runs:
        lines.append("🧾 Recent submission attempts")
        for r in runs:
            app = session.get(Application, r.application_id)
            title = f"{app.job.title} at {app.job.company_name or '?'}" if app and app.job else f"application #{r.application_id}"
            when = (r.finished_at or r.started_at)
            lines.append(f"• {title} — {_RUN_LABEL[r.status]}" + (f" ({when:%d %b %H:%M} UTC)" if when else ""))
    if events:
        lines.append("📌 Recent status changes")
        for e in events:
            app = session.get(Application, e.application_id)
            title = app.job.title if app and app.job else f"#{e.application_id}"
            lines.append(f"• {title}: {(e.from_status or 'new').replace('_', ' ')} → {e.to_status.replace('_', ' ')}"
                         f" ({e.actor})")
    lines.append(f"Details: {_dash(settings, '/applications')}")
    return "\n".join(lines)


def _open_questions(session: Session) -> list:
    from jobpilot.models import ApplicationQuestion
    from jobpilot.services.applications import OPEN_STATES

    rows = session.scalars(select(ApplicationQuestion).where(ApplicationQuestion.status == "unanswered")
                           .order_by(ApplicationQuestion.application_id, ApplicationQuestion.id))
    return [q for q in rows if q.application and q.application.status in OPEN_STATES]


def cmd_questions(session: Session, settings: Settings) -> str:
    from jobpilot.services import answers as answer_svc

    qs = _open_questions(session)
    if not qs:
        return "No application questions are waiting for you."
    lines, current = [f"❓ {len(qs)} question(s) waiting — reply /answer Q<number> <your answer>"], None
    for q in qs[:20]:
        if q.application_id != current:
            current = q.application_id
            job = q.application.job
            lines.append(f"\n#{q.application_id} {job.title} at {job.company_name or '?'}")
        if answer_svc.is_legal(q.question):
            lines.append(f"• ⚖️ legal (complete it yourself on the form): {q.question[:200]}")
        else:
            lines.append(f"• Q{q.id}{' 🔒 personal' if q.sensitive else ''}: {q.question[:200]}")
    if len(qs) > 20:
        lines.append(f"… and {len(qs) - 20} more: {_dash(settings, '/applications')}")
    return "\n".join(lines)


def cmd_answer(session: Session, settings: Settings, args: str) -> str:
    """Your answer to one application question, sent from your own chat: approved as answered by you.

    ``+bank`` at the end also saves a non-sensitive answer to the approved answer bank for future applications.
    Nothing is submitted by this command."""
    import re

    from jobpilot.models import ApplicationQuestion
    from jobpilot.services import answers as answer_svc
    from jobpilot.services.applications import OPEN_STATES

    m = re.match(r"^\s*Q?(\d+)\s+(.+?)\s*$", args or "", re.S | re.I)
    if not m:
        return "Usage: /answer Q12 your answer   (see /questions for the numbers)"
    q = session.get(ApplicationQuestion, int(m.group(1)))
    text = m.group(2)
    to_bank = text.lower().endswith("+bank")
    if to_bank:
        text = text[: -len("+bank")].strip()
    if q is None or q.application is None or q.application.status not in OPEN_STATES:
        return f"Question Q{m.group(1)} is not open. See /questions."
    if answer_svc.is_legal(q.question):
        return (f"Q{q.id} is a legal acknowledgement / consent / signature. JobPilot never answers or signs those: "
                "complete it yourself on the application form. Nothing changed.")
    if not text:
        return "The answer is empty. Nothing changed."
    answer_svc.set_answer(session, q, text)
    answer_svc.approve(session, q, answered_myself=True)  # you wrote it yourself, from your own chat
    saved = ""
    if to_bank:
        if q.sensitive:
            saved = " Not saved to the answer bank: personal/legal answers are only used for this application."
        else:
            from jobpilot.models import AnswerBankEntry
            from jobpilot.services.text_utils import normalize

            same = next((e for e in session.scalars(select(AnswerBankEntry))
                         if normalize(e.question) == normalize(q.question)), None)
            answer_svc.save_entry(session, q.question, text, category=same.category if same else "general",
                                  role_categories=same.role_categories if same else None, approved=True,
                                  entry_id=same.id if same else None)  # one entry per question, updated
            saved = " Saved to the answer bank (approved) for future applications."
    app = q.application
    left = answer_svc.unanswered_count(session, app)
    tail = f" {left} question(s) still open for #{app.id}." if left else f" All questions for #{app.id} are answered."
    return f"✅ Q{q.id} answered: “{text[:120]}”." + saved + tail


def _draft_state(d) -> str:
    if d.status == "cancelled":
        return "cancelled"
    if d.sent_at:
        state = f"sent {d.sent_at:%d %b} ({d.sent_via or 'manual'})"
        if d.reply_received_at:
            state += f", replied {d.reply_received_at:%d %b}"
        return state + (" — bounced" if d.contact and d.contact.email_status == "bounced" else "")
    if d.gmail_status == "created":
        return "in Gmail Drafts — NOT sent"
    return {"draft": "draft — needs your review", "approved": "approved — not yet in Gmail"}.get(d.status, d.status)


def cmd_recruiters(session: Session, settings: Settings) -> str:
    from jobpilot.models import Contact
    from jobpilot.services import contacts as contact_svc

    rows = list(session.scalars(select(Contact).where(Contact.role_type == "recruiter").order_by(Contact.id.desc())
                                .limit(15)))
    if not rows:
        return ("No recruiter contacts yet. Add one you found on LinkedIn or Apollo with `jobpilot recruiters add`, or "
                "import an Apollo CSV with `jobpilot recruiters import FILE`.")
    lines = [f"👤 Recruiters ({len(rows)} most recent)"]
    for c in rows:
        jobs = ", ".join(f"#{j.id}" for j, _, _ in contact_svc.jobs_for_contact(session, c)[:4]) or "no job linked"
        status = c.email_status if c.email else "unavailable"
        flags = " ⛔ do-not-contact" if c.do_not_contact else ""
        warn = contact_svc.domain_warning(c)
        lines.append(f"• {c.display_name}{' — ' + c.title if c.title else ''} · {c.company.name if c.company else '?'}"
                     f"\n   email: {c.email or 'unavailable'} ({status}) · source: {c.source_type} · jobs: {jobs}{flags}"
                     + (f"\n   ⚠️ {warn}" if warn else ""))
    lines.append(f"Email status 'verified' only when you confirmed it; 'apollo_reported' = Apollo's check, not ours. "
                 f"{_dash(settings, '/contacts')}")
    return "\n".join(lines)


def _outreach_drafts(session: Session):
    from jobpilot.services.drafts import OUTREACH_KINDS

    return list(session.scalars(select(EmailDraft).where(EmailDraft.kind.in_(OUTREACH_KINDS))
                                .order_by(EmailDraft.id.desc())))


def cmd_outreach(session: Session, settings: Settings) -> str:
    drafts = _outreach_drafts(session)[:12]
    if not drafts:
        return "No recruiter emails yet. Create one with `jobpilot outreach draft --job ID --contact ID`."
    lines = ["✉️ Recruiter emails (most recent)"]
    for d in drafts:
        job = f"job #{d.job_id}" if d.job_id else "no job"
        lines.append(f"• #{d.id} to {d.to_address or '(no recipient)'} · {job} · “{d.subject[:60]}” — {_draft_state(d)}")
    lines.append("JobPilot never sends these: you send from Gmail.")
    return "\n".join(lines)


def cmd_outreach_pending(session: Session, settings: Settings) -> str:
    from jobpilot.services import drafts as draft_svc

    pending = [d for d in _outreach_drafts(session) if d.status in ("draft", "approved") and not d.sent_at]
    if not pending:
        return "No recruiter emails are waiting for you."
    lines = [f"📝 {len(pending)} recruiter email(s) waiting"]
    for d in pending[:10]:
        warns = draft_svc.recipient_warnings(d)
        nxt = ("review and approve: `jobpilot outreach approve --draft %d`" % d.id if d.status == "draft" else
               ("open Gmail Drafts and send it yourself" if d.gmail_status == "created" else
                "put it in Gmail Drafts: `jobpilot gmail create-draft --draft %d`" % d.id))
        lines.append(f"• #{d.id} to {d.to_address or '(no recipient)'}: “{d.subject[:60]}” — {_draft_state(d)}"
                     f"\n   next: {nxt}" + "".join(f"\n   ⚠️ {w}" for w in warns[:2])
                     + (f"\n   ❗ missing: {', '.join(d.missing_fields[:4])}" if d.missing_fields else ""))
    return "\n".join(lines)


def cmd_outreach_stats(session: Session, settings: Settings) -> str:
    from jobpilot.models import Contact

    contacts = list(session.scalars(select(Contact).where(Contact.role_type == "recruiter")))
    drafts = _outreach_drafts(session)
    n = lambda pred: sum(1 for d in drafts if pred(d))  # noqa: E731
    return ("📊 Outreach\n"
            f"Recruiter contacts: {len(contacts)} (with email: {sum(1 for c in contacts if c.email)}; verified by you: "
            f"{sum(1 for c in contacts if c.email_status == 'verified')}; Apollo-reported: "
            f"{sum(1 for c in contacts if c.email_status == 'apollo_reported')})\n"
            f"Drafts awaiting review: {n(lambda d: d.status == 'draft')}\n"
            f"Approved, not in Gmail: {n(lambda d: d.status == 'approved' and d.gmail_status != 'created' and not d.sent_at)}\n"
            f"In Gmail Drafts (not sent): {n(lambda d: d.gmail_status == 'created' and not d.sent_at)}\n"
            f"Sent: {n(lambda d: d.sent_at is not None)} · Replies: {n(lambda d: d.reply_received_at is not None)}\n"
            f"Bounced addresses: {sum(1 for c in contacts if c.email_status == 'bounced')} · "
            f"Do-not-contact: {sum(1 for c in contacts if c.do_not_contact)}")


# ---- your application tracker -----------------------------------------------------------------------------

def _job_line(session: Session, settings: Settings, j: Job, i: int | None = None) -> str:
    from jobpilot.services import tracker

    star = "⭐ " if tracker.is_dream(session, j.company_name) else ""
    head = f"{i}. " if i else "• "
    return (f"{head}{star}{_ELIG.get(j.eligibility, '❓')} {j.relevance_score or 0:.0f} {j.title} — {j.company_name or '?'}"
            + (f" ({j.location[:40]})" if j.location else "") + f"\n   apply: {j.apply_url or j.source_url or '—'}")


def cmd_alljobs(session: Session, settings: Settings) -> str:
    from jobpilot.services import applications as apps, tracker

    jobs = tracker.relevant_jobs(session)
    if not jobs:
        return f"No relevant, not-ineligible active jobs right now. {_dash(settings, '/tracker')}"
    lines = [f"📜 All relevant jobs ({len(jobs)}; ⭐ dream companies first; ineligible ones left out)"]
    for j in jobs:
        app = apps.for_job(session, j.id)
        lines.append(_job_line(session, settings, j) + (f"\n   📂 #{app.id} {app.status.replace('_', ' ')}" if app else ""))
    lines.append(f"Everything, including ineligible jobs: {_dash(settings, '/tracker?all=1')}")
    return "\n".join(lines)


def cmd_track(session: Session, settings: Settings) -> str:
    from jobpilot.services import tracker

    jobs = tracker.to_ask(session, limit=3)
    if not jobs:
        return "🙋 Nothing to ask: you've told me about every relevant job. /applied shows your applications."
    return ("🙋 Did you apply to these?\n" + "\n".join(_job_line(session, settings, j, i) for i, j in enumerate(jobs, 1))
            + "\nThe buttons are numbered like the list. ✅ asks which resume you used.")


def cmd_dream(session: Session, settings: Settings) -> str:
    from jobpilot.services import applications as apps, tracker

    names = ", ".join(tracker.cfg(session)["dream_companies"]) or "none set"
    jobs = [j for j in tracker.relevant_jobs(session) if tracker.is_dream(session, j.company_name)]
    groups = [g for g in tracker.by_company(session) if g["dream"]]
    lines = [f"⭐ Dream companies: {names}", f"Relevant open jobs there: {len(jobs)}"]
    lines += [_job_line(session, settings, j, i) + (f"\n   📂 #{a.id} {a.status}" if (a := apps.for_job(session, j.id)) else "")
              for i, j in enumerate(jobs[:15], 1)]
    if len(jobs) > 15:
        lines.append(f"…and {len(jobs) - 15} more: /alljobs")
    lines += [_company_line(g) for g in groups]
    return "\n".join(lines)


def _company_line(g: dict) -> str:
    last = f"last applied {g['last_applied']:%d %b %Y}" if g["last_applied"] else "not applied yet"
    cd = (f"; cooldown {g['cooldown_days']} days → " + ("⏳ wait until " if g["in_cooldown"] else "ok since ")
          + f"{g['cooldown_until']:%d %b %Y}") if g["cooldown_until"] else ""
    return f"{'⭐ ' if g['dream'] else ''}🏢 {g['company']}: {len(g['applications'])} application(s), {last}{cd}"


def _app_line(app: Application) -> str:
    from jobpilot.services import tracker

    when = f"{app.applied_at:%d %b %Y}" if app.applied_at else "not submitted"
    how = {"self_reported": "you", "confirmation_email": "email-confirmed"}.get(app.confirmation_method or "", "")
    return (f"   • #{app.id} {app.job.title if app.job else '?'} — {app.status.replace('_', ' ')} · {when}"
            + (f" ({how})" if how and app.applied_at else "") + f" · resume: {tracker.resume_label(app)}")


def cmd_applied(session: Session, settings: Settings) -> str:
    from jobpilot.services import tracker

    groups = tracker.by_company(session)
    if not groups:
        return "🗂 No applications yet. /track asks about the jobs I found; tap ✅ when you've applied."
    lines = [f"🗂 Your applications by company ({sum(len(g['applications']) for g in groups)})"]
    for g in groups:
        lines.append(_company_line(g))
        lines += [_app_line(a) for a in g["applications"]]
    lines.append("Search one company: /company amazon · set its cooldown: /cooldown amazon 90")
    return "\n".join(lines)


def cmd_company(session: Session, settings: Settings, args: str) -> str:
    from jobpilot.services import tracker
    from jobpilot.services.text_utils import normalize_company

    q = args.strip()[:80]
    if not q:
        return "Send /company <name>, for example /company amazon"
    groups = tracker.by_company(session, q)
    jobs = [j for j in tracker.relevant_jobs(session) if normalize_company(q) in normalize_company(j.company_name)]
    lines = [f"🏢 “{q}”"]
    if groups:
        for g in groups:
            lines.append(_company_line(g))
            lines += [_app_line(a) for a in g["applications"]]
            if g["cooldown_days"] is None:
                lines.append(f"   No cooldown set. If {g['company']} has one, set it: /cooldown {q} <days>")
    else:
        lines.append("No applications there yet.")
    lines.append(f"Relevant open jobs there: {len(jobs)}")
    lines += [_job_line(session, settings, j, i) for i, j in enumerate(jobs[:10], 1)]
    return "\n".join(lines)


def cmd_cooldown(session: Session, settings: Settings, args: str) -> str:
    from jobpilot.services import tracker

    parts = args.rsplit(None, 1)
    if len(parts) != 2 or not (parts[1].isdigit() or parts[1] in ("off", "0")):
        return "Send /cooldown <company> <days> (or 'off'), e.g. /cooldown amazon 90"
    days = 0 if parts[1] in ("off", "0") else int(parts[1])
    try:
        tracker.set_cooldown(session, parts[0][:80], days or None)
    except ValueError as exc:
        return f"Not saved: {str(exc)[:200]}"
    return (f"Cooldown for {parts[0][:80]}: {days} days. /company {parts[0][:40]} shows when you can apply again."
            if days else f"Cooldown for {parts[0][:80]} removed.")


def tracker_callback(session: Session, settings: Settings, parts: list[str]) -> tuple[str, str]:
    """t:a / t:r / t:n / t:x (did you apply), o: (email outcome), u: (undo). Returns (command, reply)."""
    from jobpilot.services import applications as apps, telegram_ui, tracker

    # The caller records the update and commits; nothing here sends anything.
    if parts[0] == "o" and len(parts) == 3 and parts[1].isdigit() and parts[2] in {v for _, v in telegram_ui.OUTCOMES}:
        reply = tracker.decide_email_outcome(session, int(parts[1]), parts[2])
        return "o:decided", reply
    if parts[0] == "u" and len(parts) == 2 and parts[1].isdigit():
        app = session.get(Application, int(parts[1]))
        reply = tracker.undo_outcome(session, app) if app else "That application no longer exists."
        return "u:undo", reply
    if parts[0] != "t" or len(parts) < 3 or not parts[2].isdigit():
        return "cb:unknown", "That button is not recognised. Nothing changed."
    job = session.get(Job, int(parts[2]))
    if job is None:
        return "t:gone", "That job is no longer in JobPilot. Nothing changed."
    what = f"{job.title} — {job.company_name or '?'}"
    existing = apps.for_job(session, job.id)
    if parts[1] == "a" and len(parts) == 3:
        if existing is not None and existing.applied_at is not None:
            return "t:done", f"Already recorded: #{existing.id} applied on {existing.applied_at:%d %b %Y}. /applied"
        return f"t:a:{job.id}", f"📄 Which resume did you use?\n{what}"
    if parts[1] == "r" and len(parts) == 4 and parts[3].isdigit():
        vid = int(parts[3]) or None
        try:
            app = tracker.record_self_applied(session, job, vid, via="telegram")
        except apps.ApplicationError as exc:
            return "t:error", f"Not recorded: {str(exc)[:200]}"
        g = tracker.by_company(session, job.company_name or "")
        cd = ""
        if g and g[0]["cooldown_until"]:
            cd = f"\nCooldown at {job.company_name}: next application from {g[0]['cooldown_until']:%d %b %Y}."
        return "t:recorded", (f"✅ Recorded: you applied\n{what}\nApplication #{app.id} · resume: {tracker.resume_label(app)}"
                              "\nI'll watch your inbox (subjects only) and update it on a rejection, assessment or "
                              f"interview.{cd}")
    if parts[1] == "n" and len(parts) == 3:
        tracker.answer_not_yet(session, job)
        return "t:later", f"⏳ OK — I'll ask again in {tracker.cfg(session)['ask_again_days']} days.\n{what}"
    if parts[1] == "x" and len(parts) == 3:
        if existing is not None and existing.applied_at is not None:
            return "t:done", f"You already applied to this one (#{existing.id}); nothing changed."
        tracker.answer_not_interested(session, job)
        return "t:skip", f"🚫 Dismissed: {what}\nIt won't be suggested again."
    return "cb:unknown", "That button is not recognised. Nothing changed."


def _assistant_new(settings: Settings) -> str:
    from jobpilot.services import assistant

    return assistant.new_conversation(settings)


def _assistant_usage(settings: Settings) -> str:
    from jobpilot.services import assistant

    return assistant.usage_command(settings)


ARG_COMMANDS = {"/answer": cmd_answer, "/company": cmd_company, "/cooldown": cmd_cooldown}

MENU_TEXT = ("🧭 JobPilot\nTap a button. Views are read-only; changes happen only through Pause / Resume (outreach) "
             "and your own answers (applied, resume, not interested, what an email meant). The dashboard has "
             "everything else.")

COMMANDS = {
    "/new": lambda s, st: _assistant_new(st), "/usage": lambda s, st: _assistant_usage(st),
    "/start": lambda s, st: HELP, "/help": lambda s, st: HELP, "/menu": lambda s, st: MENU_TEXT, "/status": cmd_status,
    "/today": lambda s, st: notifications.daily_summary(s, st), "/jobs": cmd_jobs, "/pending": cmd_pending,
    "/applications": cmd_applications, "/pause": cmd_pause, "/resume": cmd_resume,
    "/outcomes": cmd_outcomes, "/questions": cmd_questions,
    "/recruiters": cmd_recruiters, "/outreach": cmd_outreach, "/outreach_pending": cmd_outreach_pending,
    "/outreach_stats": cmd_outreach_stats, "/alljobs": cmd_alljobs, "/track": cmd_track, "/dream": cmd_dream,
    "/applied": cmd_applied,
}


def handle_callback(session: Session, settings: Settings, cq: dict) -> tuple[str, str | None, str | None]:
    """A button press: (result, command, reply). Only from the configured chat AND that same user."""
    chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
    user_id = str((cq.get("from") or {}).get("id", ""))
    if not chat_id or chat_id != str(settings.telegram_chat_id) or user_id != str(settings.telegram_chat_id):
        return "rejected", "callback", None
    data = str(cq.get("data") or "")[:64]
    parts = data.split(":")
    if data in ("c:pause", "m:pause"):
        return "answered", "cb:pause", cmd_pause(session, settings)
    if data == "m:resume":
        return "answered", "cb:resume", cmd_resume(session, settings)
    from jobpilot.services import telegram_ui

    if parts[0] == "m" and len(parts) == 2 and parts[1] in telegram_ui.VIEWS:
        command = telegram_ui.VIEWS[parts[1]][1]
        handler = COMMANDS.get(command)
        if handler is not None:  # the same read-only views as the commands
            return "answered", f"m:{parts[1]}"[:40], handler(session, settings)
    if parts[0] in ("t", "o", "u"):
        command, reply = tracker_callback(session, settings, parts)
        return "answered", command[:40], reply
    if parts[0] == "q" and len(parts) == 2 and parts[1].isdigit():
        from jobpilot.models import ApplicationQuestion
        from jobpilot.services import answers as answer_svc
        from jobpilot.services.applications import OPEN_STATES

        q = session.get(ApplicationQuestion, int(parts[1]))
        if q is None or q.status != "unanswered" or q.application is None or q.application.status not in OPEN_STATES:
            return "answered", "q:closed", "That question is no longer open. See /questions."
        if answer_svc.is_legal(q.question):
            return "answered", "q:legal", ("That is a legal acknowledgement / signature: complete it yourself on the "
                                           "application form. Nothing changed.")
        job = q.application.job
        return "answered", f"q:{q.id}", (f"✍️ Answer Q{q.id} — reply to this message with your answer\n"
                                         f"Application #{q.application_id}: {job.title} at {job.company_name or '?'}\n"
                                         f"Question: {q.question[:300]}")
    return "answered", "cb:unknown", "That button is not recognised. Nothing changed."


def parse_command(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None
    return text.split()[0].split("@", 1)[0].lower()[:40]


def handle_update(session: Session, settings: Settings, update: dict) -> tuple[str, str | None, str | None]:
    """(result, command, reply text or None). Pure decision; the caller records and sends."""
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    command = parse_command(msg.get("text"))
    if not chat_id or chat_id != str(settings.telegram_chat_id):
        return "rejected", command, None
    if not msg.get("text"):
        return "ignored", None, None
    reply_to = msg.get("reply_to_message") or {}
    if command is None and reply_to and (reply_to.get("from") or {}).get("is_bot"):
        from jobpilot.services import telegram_ui

        m = telegram_ui.ANSWER_PROMPT.search(reply_to.get("text") or "")
        if m:  # your reply to an "Answer Q12" prompt is the answer to Q12 (same checks as /answer)
            return "answered", "/answer", cmd_answer(session, settings, f"Q{m.group(1)} {msg['text']}")
    if command is None:
        from jobpilot.services import assistant

        if assistant.worker_alive(settings):  # plain messages go to Claude (services/assistant.py)
            assistant.enqueue(settings, msg["text"])
            return "answered", "assistant", "🤖 Asking Claude…"
        return "answered", None, "I only understand commands. Send /menu for buttons, or /help."
    if command in ARG_COMMANDS:
        args = msg["text"].split(None, 1)[1] if len(msg["text"].split(None, 1)) > 1 else ""
        return "answered", command, ARG_COMMANDS[command](session, settings, args)
    handler = COMMANDS.get(command)
    if handler is None:
        return "answered", command, f"Unknown command {command[:30]}. Send /help for the list."
    return "answered", command, handler(session, settings)


def poll_once(settings: Settings, factory: sessionmaker[Session], client: telegram.TelegramClient | None = None,
              poll_timeout: int = POLL_TIMEOUT) -> dict[str, Any]:
    """One long poll. Raises ``TaskLocked`` if another process is polling."""
    if not telegram.is_ready(settings):
        raise telegram.TelegramError("Telegram not configured: " + "; ".join(telegram.config_status(settings)["problems"]))
    client = client or telegram.TelegramClient(settings)
    stats = {"received": 0, "answered": 0, "rejected": 0, "duplicates": 0, "errors": 0}
    with task_lock(settings, "telegram_poll"):
        with factory() as s:
            state = notifications.get_state(s, OFFSET_KEY) or {}
        updates = client.get_updates(state.get("offset"), poll_timeout)
        for u in updates:
            uid = u.get("update_id")
            if not isinstance(uid, int):
                continue
            stats["received"] += 1
            with factory() as s:
                if s.get(TelegramUpdate, uid) is not None:
                    stats["duplicates"] += 1  # already handled: just move past it
                    notifications.set_state(s, OFFSET_KEY, {"offset": uid + 1})
                    s.commit()
                else:
                    cq = u.get("callback_query")
                    if cq:
                        result, command, reply = handle_callback(s, settings, cq)
                        msg = cq.get("message") or {}
                    else:
                        result, command, reply = handle_update(s, settings, u)
                        msg = u.get("message") or {}
                    rec = TelegramUpdate(update_id=uid, chat_id=str((msg.get("chat") or {}).get("id", ""))[:40] or None,
                                         authorized=result != "rejected", command=command, result=result)
                    s.add(rec)
                    notifications.set_state(s, OFFSET_KEY, {"offset": uid + 1})
                    s.commit()  # recorded before answering: an update is never answered twice
                    if result == "rejected":
                        stats["rejected"] += 1
                        log.warning("telegram update from an unauthorised chat ignored", extra={"update_id": uid})
                    elif reply:
                        try:
                            if cq:
                                client.answer_callback(str(cq.get("id", "")), reply.splitlines()[0])
                            client.send_text(reply, reply_markup=_reply_markup(s, command), html=True)
                            stats["answered"] += 1
                        except telegram.TelegramError as exc:
                            rec.result = "error"
                            stats["errors"] += 1
                            log.warning("telegram reply failed", extra={"error": telegram.redact(str(exc), settings)})
                            s.commit()
    return stats


def _reply_markup(session: Session, command: str | None) -> dict | None:
    """Buttons under a reply: the menu, related views, posting links, answer buttons, or the reply box."""
    from jobpilot.services import telegram_ui

    try:
        if command and command.startswith("q:") and command[2:].isdigit():
            return telegram_ui.force_reply(int(command[2:]))
        if command and command.startswith("m:"):
            command = telegram_ui.VIEWS.get(command[2:], ("", None))[1]
        if command == "/start":
            return telegram_ui.reply_keyboard()
        return telegram_ui.command_markup(session, command)
    except Exception:  # buttons are a convenience: never let them stop the reply
        log.warning("telegram buttons skipped", exc_info=True)
        return None


def start_poller(settings: Settings, factory: sessionmaker[Session]) -> threading.Thread:
    """Background long-polling thread for ``jobpilot serve``. Backs off on errors; never busy-loops."""
    stop = threading.Event()

    def loop() -> None:
        delay = 0.0
        while not stop.wait(delay):
            try:
                poll_once(settings, factory)
                delay = 0.5  # the long poll itself waits up to POLL_TIMEOUT seconds for messages
            except TaskLocked:
                delay = 60.0  # another process is polling
            except telegram.TelegramError as exc:
                delay = 300.0 if exc.status in (401, 404, 409) else min(max(delay * 2, 10.0), 300.0)
                log.warning("telegram polling paused", extra={"error": telegram.redact(str(exc), settings),
                                                              "retry_in": delay})
            except Exception:  # never kill the thread
                log.exception("telegram polling error")
                delay = 60.0

    t = threading.Thread(target=loop, name="jobpilot-telegram", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    return t
