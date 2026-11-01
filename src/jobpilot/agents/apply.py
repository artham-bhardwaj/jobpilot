"""Apply agent: send or submit each application where it is safe to, and hand everything else to you ready to go.

Channels, in order:
1. **Email application**: the posting asks for applications by email ("send your CV to jobs@acme.com"). The address
   is recorded as a contact (verified by the posting itself, with the sentence quoted), and a short email built
   from your confirmed facts is sent from your Gmail with the tailored resume attached. Same checks as any send:
   blocked or bounced addresses, the daily send limit, no retry after an unknown outcome.
2. **Hosted form** (Greenhouse, Lever, Ashby; ``allowed_platforms``): the form is filled from confirmed facts,
   approved answers and the tailored resume, and submitted only when it has no CAPTCHA / bot check, login or
   verification prompt, every required field has an approved value (so required consent and legal boxes always
   come to you), and there is exactly one submit control. Every condition is checked again right before the click,
   and the attempt is recorded before clicking: an attempt with an unclear result is never repeated.
3. **Everything else** (a bot check, an unanswered question, Workday / company sites with logins, dream companies):
   an application is prepared with the resume chosen, and it goes on your "Needs you" list with the one thing to
   do (usually one command or one click).

``apply_mode: dry_run`` does all the reading and preparing (contacts, drafts, form rehearsals) but sends and
submits nothing. ``auto`` applies. The daily limit and the pause switch (Telegram /pause) hold in both.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.agents.base import Report, applied_today, is_paused, job_fits
from jobpilot.config import Settings
from jobpilot.models import (Application, ApplicationEvidence, BrowserRun, Contact, EmailDraft, Job, ReviewItem,
                             utcnow)
from jobpilot.services import applications as apps
from jobpilot.services import audit, review, storage

log = logging.getLogger(__name__)

NEEDS_YOU = "agent_needs_you"
CLICKED = ("submitting", "confirmed", "uncertain", "submit_failed")
NEVER_RETRY = ("submitting", "confirmed", "uncertain")
CREATED_NOTE = "prepared by the Apply agent"

# ---- channel 1: email applications ---------------------------------------------------------------------------

_EMAIL = r"([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
APPLY_BY_EMAIL = [
    re.compile(r"\b(?:send|e-?mail|mail|forward|submit|share|drop)\b[^.\n]{0,60}?\b(?:cv|resume|résumé|application|"
               r"profile)s?\b[^.\n@]{0,60}?" + _EMAIL, re.I),
    re.compile(r"\bapply\b[^.\n@]{0,40}?\b(?:by|via|through)\s+e-?mail\b[^.\n@]{0,30}?" + _EMAIL, re.I),
    re.compile(r"\b(?:cv|resume|résumé|applications?)s?\b[^.\n@]{0,25}?\b(?:to|at|on)\s*:?\s*" + _EMAIL, re.I),
]
NOT_FOR_APPLICATIONS = re.compile(r"privacy|gdpr|dpo|data.?protection|no-?reply|accommodat|accessib|legal|security|"
                                  r"abuse|unsubscribe|press|media|invoice|billing|support|sales", re.I)


def application_email(job: Job) -> tuple[str, str] | None:
    """(address, the sentence that asks for applications by email) or None."""
    text = job.description_text or ""
    for rx in APPLY_BY_EMAIL:
        for m in rx.finditer(text):
            addr = m.group(1).rstrip(".").lower()
            if NOT_FOR_APPLICATIONS.search(addr.split("@")[0]):
                continue
            start = max(0, text.rfind("\n", 0, m.start()) + 1)
            end = text.find("\n", m.end())
            return addr, " ".join(text[start:end if end > 0 else None].split())[:300]
    return None


def _contact_for(session: Session, job: Job, address: str, sentence: str) -> Contact:
    from jobpilot.services import contacts as contact_svc

    c = session.scalar(select(Contact).where(Contact.email == address))
    if c is None:
        c, _ = contact_svc.save_contact(session, contact_svc.ContactIn(
            email=address, company_name=job.company_name, job_id=job.id, role_type="recruiter",
            source_type="job_posting", source_url=job.source_url or job.apply_url or None,
            source_note=f"application address published in job posting #{job.id}", name="Hiring team"))
    if c.email_status not in ("verified", "bounced"):
        contact_svc.verify_email(session, c, f"the posting (job #{job.id}) asks for applications at this address: "
                                             f"“{sentence[:200]}”")
    return c


def _email_body(session: Session, job: Job) -> tuple[str, str] | None:
    """Subject and text from confirmed facts only (the same facts outreach emails use). None when your profile
    isn't confirmed: no application goes out without your name."""
    from jobpilot.services import drafts as draft_svc

    cand = draft_svc.build_context(session, job=job)["candidate"]
    name = cand["full_name"] if isinstance(cand["full_name"], str) else None
    if not name:
        return None
    at = f" at {job.company_name}" if job.company_name else ""
    lines = [f"Dear {job.company_name + ' ' if job.company_name else ''}Hiring Team,", "",
             f"I would like to apply for the {job.title} role{at}. My resume is attached."]
    about = []
    if cand.get("current_role"):
        about.append(f"I currently work as {cand['current_role']}")
    if cand.get("matching_skills"):
        about.append(f"I work with {cand['matching_skills']}, which the role asks for")
    if about:
        lines += ["", "; ".join(about) + "."]
    lines += ["", "Thank you for your time and consideration.", "", "Kind regards,", name]
    lines += [x for x in (cand.get("email"), cand.get("phone")) if x]
    lines += list(cand.get("links") or [])
    return f"Application for {job.title} — {name}"[:250], "\n".join(lines)


def _email_in_flight(session: Session, job: Job) -> EmailDraft | None:
    return session.scalar(select(EmailDraft).where(EmailDraft.job_id == job.id, EmailDraft.kind == "application",
                                                   EmailDraft.status != "cancelled").order_by(EmailDraft.id.desc()))


def email_apply(session: Session, settings: Settings, job: Job, app: Application, resume: dict, address: str,
                sentence: str, mode: str, client=None) -> tuple[str, str]:
    """(outcome, detail) for one email application."""
    from jobpilot.integrations import gmail
    from jobpilot.services import contacts as contact_svc
    from jobpilot.services import drafts as draft_svc

    blocked = contact_svc.is_blocked(session, address, None) or gmail.reserved_recipient(address)
    if blocked:
        return "needs_you", f"the posting's address {address} can't be used: {blocked}"
    draft = _email_in_flight(session, job)
    if draft is not None and (draft.status == "sent" or draft.gmail_status in ("sending", "send_unknown", "sent")):
        return "skipped", f"an application email for this job was already sent or attempted (draft #{draft.id})"
    contact = _contact_for(session, job, address, sentence)
    if draft is None:
        text = _email_body(session, job)
        if text is None:
            return "needs_you", "your profile isn't confirmed, so no application email can be written (Profile)"
        subject, body = text
        tr, version = resume.get("tailored"), resume.get("version")
        draft = EmailDraft(kind="application", contact_id=contact.id, job_id=job.id, application_id=app.id,
                           resume_version_id=(version.id if version else (tr.source_version_id if tr else None)),
                           tailored_resume_id=tr.id if tr else None, to_address=address, subject=subject, body=body,
                           status="draft")
        session.add(draft)
        session.flush()
        audit.record(session, "agents.email_drafted", "email_draft", draft.id, actor="system", job_id=job.id)
    if mode != "auto":
        return "would_email", f"would email {address} (draft #{draft.id}; {sentence[:120]})"
    if not gmail.is_ready(settings) and client is None:
        return "needs_you", f"Gmail isn't connected, so the application email to {address} (draft #{draft.id}) " \
                            "wasn't sent. Connect Gmail or send it yourself from the Email drafts page"
    if draft.status == "draft":
        try:
            draft_svc.approve(session, draft)
        except draft_svc.DraftError as exc:
            return "needs_you", f"draft #{draft.id} can't be sent: {exc}"
    session.commit()
    client = client or gmail.GmailClient(settings)
    try:
        res = gmail.send_via_gmail(session, settings, draft, client, confirm="SEND")
    except gmail.GmailError as exc:
        session.commit()
        return "needs_you", f"sending draft #{draft.id} to {address} failed: {exc}"
    app.applied_at = utcnow()
    app.confirmation_method = "email_sent"
    app.confirmation_note = f"application emailed to {address} by the Apply agent (Gmail message {res.get('id')})"
    session.add(ApplicationEvidence(application_id=app.id, kind="note",
                                    text=f"Application email sent to {address} via Gmail, message id {res.get('id')}, "
                                         f"subject “{draft.subject}”; attachment: {(draft.gmail_attachment or {}).get('filename')}"))
    apps._event(session, app, "applied", f"emailed to {address} by the Apply agent", actor="system")  # noqa: SLF001
    if job.user_status != "shortlisted":
        job.user_status = "shortlisted"
    audit.record(session, "agents.email_sent", "application", app.id, actor="system", to=address, draft_id=draft.id)
    session.commit()
    return "applied_email", f"emailed {address} with {'the tailored' if resume.get('tailored') else 'your approved'} resume"


# ---- channel 2: hosted forms -----------------------------------------------------------------------------------

def _url(app: Application) -> str | None:
    return app.apply_url or app.job.apply_url or app.job.source_url


def platform_of(url: str | None) -> str:
    from jobpilot.automation.apply import site_for

    return site_for(url) or (urlsplit(url or "").hostname or "unknown")


def preflight(session: Session, settings: Settings, app: Application, pol: dict[str, Any], mode: str) -> list[str]:
    """Every condition for clicking submit on ``app`` now. [] = go."""
    from jobpilot.services import resumes as resume_svc

    problems = []
    if mode != "auto":
        problems.append("dry run")
    if is_paused(session):
        problems.append("applications are paused")
    if not settings.browser_enabled:
        problems.append("browser automation is disabled (JOBPILOT_BROWSER_ENABLED)")
    if app.applied_at is not None or app.status in ("withdrawn", "applied"):
        problems.append(f"application is {app.status}")
    if any(r.status in NEVER_RETRY for r in app.browser_runs):
        problems.append("an earlier attempt may have submitted it")
    if platform_of(_url(app)) not in pol["allowed_platforms"]:
        problems.append("platform not allowed")
    problems += job_fits(app.job, pol)
    try:
        resume_svc.verified_file(settings, app.resume_version)
    except resume_svc.ResumeFileError as exc:
        problems.append(f"resume: {exc}")
    if applied_today(session) >= int(pol["daily_limit"]):
        problems.append(f"daily limit reached ({pol['daily_limit']})")
    return problems


def form_apply(session: Session, settings: Settings, app: Application, pol: dict[str, Any], mode: str,
               runner=None, rehearse=None) -> tuple[str, str]:
    """(outcome, detail) for one hosted-form application."""
    from jobpilot.automation import apply as browser
    from jobpilot.services import resumes as resume_svc
    from jobpilot.services import tailored

    url = _url(app)
    answer_for = browser.answer_resolver(session, app, bool(pol["use_answer_bank"]))
    if mode != "auto":
        if not settings.browser_enabled:
            return "would_form", f"would fill the {platform_of(url)} form (browser disabled, form not checked)"
        try:
            resume_svc.verified_file(settings, app.resume_version)
            resume_ok = True
        except resume_svc.ResumeFileError:
            resume_ok = False
        plan = (rehearse or browser.rehearse_form)(settings, url, browser.profile_values(session), resume_ok, answer_for,
                                                   headless=True)
        plan.pop("png", None)
        if plan.get("stage") != "planned":
            return "needs_you", f"form not checked: {plan.get('detail')}"
        if plan.get("would_submit_unattended"):
            return "would_submit", f"form ready: every required field has an approved value ({url})"
        return "needs_you", _form_next_step(app, plan.get("verdict") or "", plan.get("blockers") or [],
                                            plan.get("missing") or [], plan.get("needs_you") or [])
    problems = preflight(session, settings, app, pol, mode)
    if problems:
        return "skipped", "; ".join(problems)
    run = BrowserRun(application_id=app.id, status="running", site=platform_of(url))
    session.add(run)
    session.commit()

    def before_click() -> bool:
        session.refresh(app)
        if preflight(session, settings, app, pol, mode):
            return False
        run.status = "submitting"
        session.commit()  # on record before the click: a crash now never leads to a second submission
        return True

    try:
        resume_path, _ = tailored.attachment_for(session, settings, app)
    except resume_svc.ResumeFileError as exc:
        run.status, run.finished_at, run.message = "failed", utcnow(), str(exc)
        session.commit()
        return "needs_you", f"resume file problem: {exc}"
    result = (runner or browser.run_submission)(settings, url, browser.profile_values(session), resume_path, answer_for,
                                                before_click, headless=True)
    stage = result.get("stage")
    run.finished_at = utcnow()
    run.filled_fields = result.get("filled")
    run.unknown_fields = (result.get("missing") or []) + [f"[you answer] {s}" for s in result.get("sensitive") or []]
    run.message = result.get("detail")
    if stage == "submitted" or (stage == "error" and run.status == "submitting"):
        return _record_outcome(session, settings, app, run, result.get("outcome") if stage == "submitted" else "uncertain",
                               result)
    run.status = "paused" if stage in ("blocked", "incomplete") else ("vetoed" if stage == "vetoed" else "failed")
    session.commit()
    if stage == "incomplete":
        from jobpilot.services import answers as answer_svc

        answer_svc.add_questions(session, app, (result.get("missing") or []) + (result.get("sensitive") or []),
                                 source="browser")
    if stage == "vetoed":
        return "skipped", "not submitted: a check failed right before the click"
    return "needs_you", _form_next_step(app, result.get("detail") or stage or "", [result.get("detail") or ""]
                                        if stage == "blocked" else [], result.get("missing") or [],
                                        result.get("sensitive") or [])


def _form_next_step(app: Application, verdict: str, blockers: list[str], missing: list[str], sensitive: list[str]) -> str:
    host = urlsplit(_url(app) or "").hostname or "<site>"
    if blockers and any(blockers):
        kind = "an invisible bot check" if any("invisible" in b for b in blockers) else (
            "a login" if any("login" in b for b in blockers) else "a CAPTCHA / bot check")
        return (f"The form has {kind}, which JobPilot never gets past. Run `jobpilot assist {app.id} --approve-host "
                f"{host}`: it opens filled with your resume; complete the check and click Submit")
    if missing or sensitive:
        qs = "; ".join((missing + sensitive)[:6])
        return (f"The form asks questions without an approved answer ({qs}). Answer them on application #{app.id} "
                "(or /answer in Telegram); the next cycle continues on its own")
    return f"The form can't be submitted automatically ({verdict[:200]}). Run `jobpilot assist {app.id} " \
           f"--approve-host {host}` and submit it yourself"


def _png(settings: Settings, data: bytes | None, name: str) -> str | None:
    return storage.store(settings, "browser", f"{name}.png", data).rel_path if data else None


def _record_outcome(session: Session, settings: Settings, app: Application, run: BrowserRun, outcome: str | None,
                    result: dict[str, Any]) -> tuple[str, str]:
    before = _png(settings, result.get("before_png"), f"app{app.id}-run{run.id}-before-submit")
    after = _png(settings, result.get("after_png"), f"app{app.id}-run{run.id}-after-submit")
    run.screenshot_path = after or before
    if outcome == "confirmed":
        run.status = "confirmed"
        session.add(ApplicationEvidence(application_id=app.id, kind="confirmation_page", file_path=after,
                                        original_filename="confirmation.png" if after else None,
                                        text=f"[captured after submission at {result.get('url')}]\n"
                                             f"{(result.get('text') or '')[:5000]}"))
        app.applied_at = utcnow()
        app.confirmation_method = "confirmation_page"
        app.confirmation_note = f"submitted by the Apply agent; {result.get('detail')}"
        apps._event(session, app, "applied", "submitted by the Apply agent; confirmation page verified",  # noqa: SLF001
                    actor="system")
        if app.job.user_status != "shortlisted":
            app.job.user_status = "shortlisted"
        audit.record(session, "agents.form_submitted", "application", app.id, actor="system",
                     resume_version_id=app.resume_version_id)
        session.commit()
        return "applied_form", f"submitted on {run.site}; confirmation page saved"
    if outcome == "failed":
        run.status = "submit_failed"
        audit.record(session, "agents.form_rejected", "application", app.id, actor="system")
        session.commit()
        return "needs_you", (f"the form rejected the submission ({result.get('detail')}). Fix it on application "
                             f"#{app.id}, then dismiss this item to let the agent try again")
    run.status = "uncertain"
    if before or after:
        session.add(ApplicationEvidence(application_id=app.id, kind="file", file_path=after or before,
                                        text=f"[after submit, outcome uncertain] {result.get('detail')}"))
    audit.record(session, "agents.form_uncertain", "application", app.id, actor="system")
    session.commit()
    return "needs_you", (f"submitted, but the result is unclear ({result.get('detail')}). It will not be submitted "
                         "again: check your email or the employer's portal, then record it or withdraw it")


def recover(session: Session) -> int:
    """Attempts left 'submitting' by a crash: the click may have happened, so they become uncertain."""
    n = 0
    stale = utcnow() - timedelta(minutes=15)
    for run in session.scalars(select(BrowserRun).where(BrowserRun.status.in_(("submitting", "running")),
                                                        BrowserRun.started_at < stale)):
        clicked = run.status == "submitting"
        run.status, run.finished_at = ("uncertain" if clicked else "failed"), utcnow()
        run.message = "Interrupted (crash or stop) " + ("after the submit click may have happened" if clicked
                                                         else "before submitting")
        if clicked:
            needs_you(session, run.application, "uncertain", run.message + ". It will not be submitted again: check "
                      "your email or the employer's portal, then record it or withdraw it.")
        n += 1
    return n


# ---- hand-offs -------------------------------------------------------------------------------------------------

def needs_you(session: Session, app: Application, code: str, detail: str) -> ReviewItem:
    return review.open_item(session, NEEDS_YOU, "application", app.id,
                            f"Needs you: {app.job.title} at {app.job.company_name or '?'}", detail[:2000],
                            {"code": code, "job_id": app.job_id, "url": _url(app)})


def open_item(session: Session, app_id: int) -> ReviewItem | None:
    return session.scalar(select(ReviewItem).where(ReviewItem.kind == NEEDS_YOU, ReviewItem.entity_id == app_id,
                                                   ReviewItem.status == "open"))


def ensure_application(session: Session, job: Job, resume: dict | None) -> Application:
    app = apps.for_job(session, job.id)
    if app is None:
        dup = apps.find_duplicates(session, job)
        app = apps.create(session, job, status="preparing", actor="system", note=CREATED_NOTE,
                          override_note="prepared by the Apply agent (similar application exists)" if dup else None)
    if resume and resume.get("version") is not None and app.resume_version_id is None:
        app.resume_version_id = resume["version"].id
        app.resume_selection_reason = "chosen by the Resume agent"
    return app


def _resume_note(resume: dict) -> str:
    tr = resume.get("tailored")
    return (f"Tailored resume #{tr.id} is ready (Jobs → this job → Resume)." if tr is not None
            else "Your approved resume is selected.")


def run(session: Session, settings: Settings, pol: dict[str, Any], mode: str, jobs: list[Job], resumes: dict[int, Any],
        held: list[Job], *, gmail_client=None, runner=None, rehearse=None) -> Report:
    """Apply to ``jobs`` (each with a resume from the Resume agent) and prepare ``held`` ones for you."""
    rep = Report("apply")
    paused = is_paused(session)
    for job in held:
        try:
            app = ensure_application(session, job, resumes.get(job.id))
        except apps.ApplicationError as exc:
            rep.item(job, "skipped", str(exc))
            continue
        detail = (f"Dream company: kept for you to review. {_resume_note(resumes.get(job.id) or {})} Apply at "
                  f"{job.apply_url or job.source_url}, then press ✓ I applied on the job page")
        needs_you(session, app, "dream_company", detail)
        rep.item(job, "needs_you", detail, application_id=app.id)
        session.commit()
    for job in jobs:
        resume = resumes.get(job.id)
        if resume is None:
            rep.item(job, "no_resume", "no usable resume (see the Resume agent)")
            continue
        if mode == "auto" and applied_today(session) >= int(pol["daily_limit"]):
            rep.item(job, "deferred", f"daily limit reached ({pol['daily_limit']}); next cycle")
            continue
        if mode == "auto" and paused:
            rep.item(job, "deferred", "applications are paused (/resume in Telegram or the Agents page)")
            continue
        try:
            app = ensure_application(session, job, resume)
        except apps.ApplicationError as exc:
            session.rollback()
            rep.item(job, "skipped", str(exc))
            continue
        session.commit()
        outcome, detail = _apply_one(session, settings, pol, mode, job, app, resume, gmail_client, runner, rehearse)
        if outcome == "needs_you":
            needs_you(session, app, "form" if "form" in detail or "assist" in detail else "other",
                      f"{detail}. {_resume_note(resume)}")
        rep.item(job, outcome, detail, application_id=app.id)
        session.commit()
    c = rep.counts
    applied = c.get("applied_email", 0) + c.get("applied_form", 0)
    rep.summary = (("DRY RUN: " if mode != "auto" else "") + (
        f"{c.get('would_email', 0)} would be emailed, {c.get('would_submit', 0)} form(s) ready to submit" if mode != "auto"
        else f"{applied} application(s) sent") + f"; {c.get('needs_you', 0)} need you"
        + (f"; {c['deferred']} deferred" if c.get("deferred") else ""))
    return rep


def _apply_one(session, settings, pol, mode, job, app, resume, gmail_client, runner, rehearse) -> tuple[str, str]:
    found = application_email(job) if pol["email_applications"] else None
    if found:
        return email_apply(session, settings, job, app, resume, found[0], found[1], mode, gmail_client)
    url = _url(app)
    platform = platform_of(url)
    if pol["form_submissions"] and platform in pol["allowed_platforms"]:
        return form_apply(session, settings, app, pol, mode, runner, rehearse)
    from jobpilot.automation.apply import platform_note

    return "needs_you", (f"Apply on the site yourself ({platform_note(url)}): {url or 'no link'}; then press "
                         "✓ I applied on the job page")
