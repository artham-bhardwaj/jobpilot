"""Your application tracker: what you applied to (yourself or through JobPilot), with which resume, per company.

- "Did you apply?" — for each relevant job you answer ✅ applied (then: which resume), ⏳ not yet (asked again after
  ``ask_again_days``), or 🚫 not interested (the job is dismissed). Your answer is recorded as an application with
  confirmation method ``self_reported`` (your statement is the evidence), kept apart from submissions JobPilot verified.
- Companies: applications grouped by company, the last date you applied there, and a cooldown you set per company
  (companies don't publish these reliably, so JobPilot never assumes one).
- Dream companies (FAANG by default) are marked ⭐ and listed first.
- Employer emails (headers only, Gmail metadata scope) move an application forward when the subject is unambiguous
  (rejection, assessment, interview, offer), with an undo; anything else from that employer is asked about.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Application, ApplicationEvidence, Job, ResumeVersion, utcnow
from jobpilot.services import applications as apps
from jobpilot.services import audit, notifications, settings_store
from jobpilot.services.text_utils import normalize_company

CHECKS_KEY = "tracker.apply_checks"  # {job_id: {"answer": "not_yet"|"skip", "at": iso}}

# The 'tracker' settings (defaults in settings_store.DEFAULTS["tracker"]).
FAANG = ["Meta", "Facebook", "Apple", "Amazon", "Netflix", "Google", "Alphabet"]


def cfg(session: Session) -> dict:
    return settings_store.get(session, "tracker")


def is_dream(session: Session, company: str | None) -> bool:
    c = normalize_company(company)
    return bool(c) and any(_same_company(d, c) for d in cfg(session)["dream_companies"])


def _same_company(name: str, normalized: str) -> bool:
    """"Amazon" matches "Amazon", "Amazon Web Services", "Amazon Development Centre India" (whole words)."""
    d = normalize_company(name)
    return bool(d) and (normalized == d or normalized.startswith(d + " "))


def _checks(session: Session) -> dict:
    return dict(notifications.get_state(session, CHECKS_KEY) or {})


def relevant_jobs(session: Session, include_ineligible: bool = False) -> list[Job]:
    """Active jobs worth your attention (not ineligible unless asked), dream companies first, then by score."""
    threshold = settings_store.get(session, "matching")["relevance_threshold"]
    rows = session.scalars(select(Job).where(Job.status == "active", Job.user_status != "dismissed"))
    jobs = [j for j in rows if include_ineligible or ((j.relevance_score or 0) >= threshold
                                                     and j.eligibility != "ineligible" and not j.hard_conflict)]
    return sorted(jobs, key=lambda j: (not is_dream(session, j.company_name), (j.company_name or "").lower(),
                                       -(j.relevance_score or 0)))


def to_ask(session: Session, limit: int = 3, min_age_hours: float = 0) -> list[Job]:
    """Relevant jobs you haven't told JobPilot about (no application, not answered recently).

    ``min_age_hours``: only jobs found at least that long ago (the scheduled questions give you a day to apply;
    a new high match already carries an "I applied" button)."""
    checks, again = _checks(session), int(cfg(session)["ask_again_days"])
    found_before = utcnow() - timedelta(hours=min_age_hours)
    out = []
    for j in relevant_jobs(session):
        if apps.for_job(session, j.id) is not None or (min_age_hours and j.first_seen_at > found_before):
            continue
        c = checks.get(str(j.id))
        if c and (c["answer"] == "skip" or datetime.fromisoformat(c["at"]) > utcnow() - timedelta(days=again)):
            continue
        out.append(j)
        if len(out) >= limit:
            break
    return out


def answer_not_yet(session: Session, job: Job) -> None:
    checks = _checks(session)
    checks[str(job.id)] = {"answer": "not_yet", "at": utcnow().isoformat(timespec="seconds")}
    notifications.set_state(session, CHECKS_KEY, checks)
    audit.record(session, "tracker.not_yet", "job", job.id)


def answer_not_interested(session: Session, job: Job) -> None:
    from jobpilot.services import jobs as job_svc

    checks = _checks(session)
    checks[str(job.id)] = {"answer": "skip", "at": utcnow().isoformat(timespec="seconds")}
    notifications.set_state(session, CHECKS_KEY, checks)
    job_svc.set_user_status(session, job, "dismissed")
    audit.record(session, "tracker.not_interested", "job", job.id)


def record_self_applied(session: Session, job: Job, resume_version_id: int | None, *, resume_note: str | None = None,
                        applied_on: date | None = None, via: str = "telegram") -> Application:
    """You applied yourself: recorded as applied with your statement as the evidence (method ``self_reported``).

    ``resume_version_id``: the approved version you used, or None with ``resume_note`` ("another resume")."""
    if resume_version_id is not None:
        v = session.get(ResumeVersion, resume_version_id)
        if v is None or v.status != "approved":
            raise apps.ApplicationError("That resume version is not one of your approved resumes")
    app = apps.for_job(session, job.id)
    if app is not None and app.applied_at is not None:
        raise apps.ApplicationError(f"Already recorded as applied on {app.applied_at:%d %b %Y} (#{app.id})")
    if app is None:
        app = apps.create(session, job, status="shortlisted", note="you said you applied", actor="user",
                          override_note="you applied yourself" if apps.find_duplicates(session, job) else None)
    when = datetime.combine(applied_on, datetime.min.time()) if applied_on else utcnow()
    if resume_version_id is not None:
        app.resume_version_id = resume_version_id
        app.resume_selection_reason = "you said you used this resume"
    used = (f"{app.resume_version.resume.name} v{app.resume_version.version_no}" if app.resume_version_id
            and app.resume_version else (resume_note or "another resume (not in JobPilot)"))
    session.add(ApplicationEvidence(application_id=app.id, kind="note",
                                    text=f"You reported applying on {when:%d %b %Y} via {via}; resume: {used}."))
    app.applied_at = when
    app.confirmation_method = "self_reported"
    app.confirmation_note = f"you reported it ({via}); resume: {used}"
    apps._event(session, app, "applied", f"you reported applying ({via}); resume: {used}")  # noqa: SLF001
    if job.user_status != "shortlisted":
        job.user_status = "shortlisted"
    review_close(session, app)
    audit.record(session, "application.self_reported", "application", app.id, resume_version_id=resume_version_id,
                 via=via)
    return app


def review_close(session: Session, app: Application) -> None:
    from jobpilot.services import review

    for kind in ("submission_approval", "browser_paused"):
        review.resolve_items(session, kind, "application", app.id, "you applied")


# ---- companies and cooldowns --------------------------------------------------------------------------------------

def cooldown_days(session: Session, company: str | None) -> int | None:
    c = normalize_company(company)
    for name, days in (cfg(session)["cooldown_days"] or {}).items():
        if c and _same_company(name, c):
            return int(days)
    return None


def set_cooldown(session: Session, company: str, days: int | None) -> dict:
    c = cfg(session)
    table = {k: v for k, v in (c["cooldown_days"] or {}).items() if normalize_company(k) != normalize_company(company)}
    if days:
        table[company.strip()] = int(days)
    value = {**c, "cooldown_days": table}
    settings_store.validate("tracker", value)
    settings_store.put(session, "tracker", value)
    audit.record(session, "tracker.cooldown_set", "company", None, company=company[:100], days=days)
    return table


def by_company(session: Session, query: str | None = None) -> list[dict[str, Any]]:
    """Applications grouped by company (dream companies first), with the last application and cooldown status."""
    q = normalize_company(query) if query else None
    groups: dict[str, dict[str, Any]] = {}
    for app in session.scalars(select(Application).order_by(Application.id)):
        company = app.job.company_name if app.job else None
        key = normalize_company(company) or "?"
        if q and q not in key:
            continue
        g = groups.setdefault(key, {"company": company or "?", "dream": is_dream(session, company), "applications": [],
                                    "last_applied": None})
        g["applications"].append(app)
        if app.applied_at and (g["last_applied"] is None or app.applied_at > g["last_applied"]):
            g["last_applied"] = app.applied_at
    for g in groups.values():
        days = cooldown_days(session, g["company"])
        g["cooldown_days"] = days
        g["cooldown_until"] = (g["last_applied"] + timedelta(days=days)) if days and g["last_applied"] else None
        g["in_cooldown"] = bool(g["cooldown_until"] and g["cooldown_until"] > utcnow())
    return sorted(groups.values(), key=lambda g: (not g["dream"], g["company"].lower()))


def resume_label(app: Application) -> str:
    if app.resume_version_id and app.resume_version:
        return f"{app.resume_version.resume.name} v{app.resume_version.version_no}"
    m = re.search(r"resume: (.+)$", app.confirmation_note or "")
    return m.group(1) if m else "—"


# ---- employer emails → outcomes ------------------------------------------------------------------------------------

OUTCOME_SUBJECTS = [  # (status, pattern) — clear wording only; anything else from the employer is asked about
    ("rejected", re.compile(r"unfortunately|we regret|not (?:be )?moving forward|not (?:been )?selected|"
                            r"other candidates|decided not to proceed|position has been filled|no longer under consideration",
                            re.I)),
    ("offer", re.compile(r"\boffer (?:letter|of employment)\b|pleased to offer|job offer", re.I)),
    ("interview", re.compile(r"interview (?:invitation|invite|request|schedul)|invitation to interview|"
                             r"schedule (?:your|an|a) (?:interview|call)|phone screen", re.I)),
    ("assessment", re.compile(r"assessment|coding (?:challenge|test|exercise)|online test|hackerrank|codility|"
                              r"codesignal|take-?home", re.I)),
]
ALLOWED = {"applied": {"rejected", "offer", "interview", "assessment"}, "assessment": {"rejected", "offer", "interview"},
           "interview": {"rejected", "offer", "assessment"}}


def classify_subject(subject: str) -> str | None:
    for status, rx in OUTCOME_SUBJECTS:
        if rx.search(subject or ""):
            return status
    return None


def apply_outcome(session: Session, app: Application, status: str, evidence: str, actor: str = "system") -> bool:
    """Move a submitted application forward from an employer email (or your choice). Returns False if not allowed."""
    if status not in ALLOWED.get(app.status, set()):
        return False
    session.add(ApplicationEvidence(application_id=app.id, kind="note", text=evidence[:5000]))
    apps._event(session, app, status, f"from an employer email: {evidence.splitlines()[0][:150]}", actor=actor)  # noqa: SLF001
    if status in apps.CLOSED_STATES:
        app.closed_at = utcnow()
        apps._suppress_followups(session, app, f"application {status}")  # noqa: SLF001
    audit.record(session, "application.outcome_from_email", "application", app.id, actor=actor, status=status)
    return True


def undo_outcome(session: Session, app: Application) -> str:
    """Undo an automatic outcome: back to the status before it (rejected → applied etc.)."""
    from jobpilot.models import ApplicationEvent

    last = session.scalar(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
                          .order_by(ApplicationEvent.id.desc()).limit(1))
    if last is None or last.actor != "system" or not (last.note or "").startswith("from an employer email"):
        return "Nothing to undo: the last change was not made automatically from an email."
    back = last.from_status or "applied"
    apps._event(session, app, back, "undone by you (the email was misread)", actor="user")  # noqa: SLF001
    app.closed_at = None
    return f"Undone: #{app.id} is back to {back}."


OUTCOME_SEEN_KEY = "tracker.outcome_seen"
# A subject must be about a job application before anything is concluded from it (shopping or account mail from the
# same company is ignored).
JOBISH = re.compile(r"applica|applied|candida|position|\brole\b|opportunit|recruit|hiring|interview|requisition|"
                    r"\bjob\b|assessment|\boffer\b|career", re.I)


def detect_outcome_emails(session: Session, settings, client, max_messages: int = 50) -> dict:
    """Employer emails about applications you submitted (headers only — From / Subject / Date; never the body).

    For applications in applied / assessment / interview: a new inbox message whose subject is about a job application
    and that names the company (subject, sender name or sender domain) is either
    - applied automatically when its subject is unambiguous (rejection, assessment, interview, offer) and exactly one
      application at that company is open — an Undo button comes with the notification; or
    - asked about (review item ``email_outcome`` with buttons) otherwise. Confirmation emails are left alone.
    Each message is looked at once."""
    from datetime import timezone
    from email.utils import parseaddr, parsedate_to_datetime

    from jobpilot.integrations import gmail
    from jobpilot.services import review

    gmail.require_scope(settings, gmail.SCOPE_METADATA, "Tracking employer emails about your applications")
    open_apps = [a for a in session.scalars(select(Application).where(Application.applied_at.is_not(None)))
                 if a.status in ALLOWED and a.job is not None and normalize_company(a.job.company_name)]
    if not open_apps:
        return {"checked": 0, "updated": 0, "asked": 0, "message": "no submitted applications to track"}
    auto = bool(cfg(session)["auto_outcomes"])
    seen = list((notifications.get_state(session, OUTCOME_SEEN_KEY) or {}).get("ids") or [])
    checked = updated = asked = 0
    for m in client.recent_inbox(max_messages):
        if m["id"] in seen:
            continue
        seen.append(m["id"])
        checked += 1
        h = client.message_headers(m["id"])
        subject, sender = h.get("Subject", ""), h.get("From", "")
        if not JOBISH.search(subject):
            continue
        name, addr = parseaddr(sender)
        domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
        try:
            when = parsedate_to_datetime(h.get("Date", "")).astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        text = f" {normalize_company(f'{subject} {name} {domain}')} "
        hits = [a for a in open_apps if f" {normalize_company(a.job.company_name)} " in text
                and when >= a.applied_at - timedelta(days=1)]
        if not hits:
            continue
        status = classify_subject(subject)
        if status is None and gmail.CONFIRM_SUBJECT.search(subject) and not gmail.NOT_CONFIRM.search(subject):
            continue  # "Thanks for applying": nothing changes
        evidence = (f"[employer email, detected automatically from Gmail message headers; message id {m['id']}]\n"
                    f"From: {sender}\nSubject: {subject}\nDate: {h.get('Date', '')}")
        if auto and status and len(hits) == 1 and apply_outcome(session, hits[0], status, f"{subject}\n{evidence}"):
            updated += 1
            continue
        app = max(hits, key=lambda a: a.applied_at)
        others = [a for a in hits if a.id != app.id]
        review.open_item(session, "email_outcome", "application", app.id,
                         f"Email from {app.job.company_name} — what happened?",
                         f"“{subject[:200]}” from {sender[:120]} on {when:%d %b %Y}. "
                         + (f"It may be about one of {len(hits)} applications there (#{', #'.join(str(a.id) for a in hits)}); "
                            "this asks about the latest. " if others else "")
                         + (f"It looks like: {status}. " if status else "The subject doesn't say. ")
                         + "Choose what it means; nothing changed yet.",
                         data={"subject": subject[:300], "from": sender[:200], "message_id": m["id"], "guess": status})
        asked += 1
    notifications.set_state(session, OUTCOME_SEEN_KEY, {"ids": seen[-1000:]})
    return {"checked": checked, "updated": updated, "asked": asked,
            "message": f"{updated} application(s) updated from employer emails, {asked} to ask you about "
                       f"({checked} new message(s))"}


def decide_email_outcome(session: Session, item_id: int, status: str) -> str:
    """Your answer to an 'Email from X — what happened?' question."""
    from jobpilot.models import ReviewItem
    from jobpilot.services import review

    item = session.get(ReviewItem, item_id)
    if item is None or item.kind != "email_outcome" or item.status != "open":
        return "That question is already answered. Nothing changed."
    app = session.get(Application, item.entity_id)
    if app is None:
        return "That application no longer exists. Nothing changed."
    data = item.data or {}
    if status == "none":
        review.resolve(session, item.id, "dismissed", "you said: nothing changed")
        return f"Noted: no change for #{app.id} {app.job.title} at {app.job.company_name}."
    evidence = (f"You said this email means: {status}\nFrom: {data.get('from', '')}\nSubject: {data.get('subject', '')}"
                f"\n[Gmail message id {data.get('message_id', '?')}; headers only]")
    if not apply_outcome(session, app, status, evidence, actor="user"):
        return f"#{app.id} is {app.status.replace('_', ' ')}; it can't move to {status} from there. Nothing changed."
    review.resolve(session, item.id, "resolved", f"you said: {status}")
    return f"Updated: #{app.id} {app.job.title} at {app.job.company_name} → {status}."


# ---- notifications ---------------------------------------------------------------------------------------------

def events(session: Session, settings) -> list[dict[str, Any]]:
    """Notification events: 'Did you apply?' questions (a few per day), employer-email outcomes and questions."""
    from jobpilot.models import ApplicationEvent, Notification, ReviewItem

    out: list[dict[str, Any]] = []
    c = cfg(session)
    today = notifications._local_midnight_utc(utcnow())  # noqa: SLF001
    asked_today = session.query(Notification).filter(Notification.kind == "apply_check",
                                                     Notification.created_at >= today).count()
    room = max(0, int(c["ask_daily"]) - asked_today)
    if room:
        checks = _checks(session)
        for j in to_ask(session, limit=room, min_age_hours=24):
            again = checks.get(str(j.id), {}).get("at", "first")[:10]
            star = "⭐ " if is_dream(session, j.company_name) else ""
            out.append({"key": f"track:{j.id}:{again}", "kind": "apply_check", "single": True, "job_id": j.id,
                        "url": j.apply_url or j.source_url,
                        "text": f"🙋 Did you apply? {star}{j.company_name or '?'}\n{j.title}\n"
                                f"Score: {j.relevance_score or 0:.0f} · {j.eligibility.replace('_', ' ')}"
                                + (f" · {j.location}" if j.location else "")
                                + f"\nApply: {j.apply_url or j.source_url or '—'}\n"
                                  "Tap ✅ if you applied (then pick the resume), ⏳ to be asked again later, "
                                  "🚫 if you're not interested."})
    for ev in session.scalars(select(ApplicationEvent).where(ApplicationEvent.actor == "system",
                                                             ApplicationEvent.note.like("from an employer email%"))):
        app = ev.application
        if app is None or app.job is None:
            continue
        out.append({"key": f"appevent:{ev.id}", "kind": "email_outcome", "single": True, "app_id": app.id,
                    "undo": True,
                    "text": f"📬 {app.job.company_name}: application updated → {ev.to_status}\n"
                            f"{app.job.title} (#{app.id})\nEmail: {ev.note.split(':', 1)[-1].strip()[:200]}\n"
                            "Read from the subject only. Wrong? Tap Undo."})
    for item in session.scalars(select(ReviewItem).where(ReviewItem.kind == "email_outcome", ReviewItem.status == "open")):
        app = session.get(Application, item.entity_id)
        if app is None:
            continue
        out.append({"key": f"review:{item.id}", "kind": "email_outcome", "single": True, "review_id": item.id,
                    "guess": (item.data or {}).get("guess"),
                    "text": f"📬 {item.title}\n{app.job.title} (#{app.id}, {app.status.replace('_', ' ')})\n"
                            f"{item.reason or ''}"})
    return out


def is_email_outcome_event(ev) -> bool:
    return ev.actor == "system" and (ev.note or "").startswith("from an employer email")
