"""Application pipeline.

Lifecycle::

    discovered → shortlisted → preparing → ready_for_review ──(approve + confirm)──▶ applied
                                                          applied → assessment → interview → offer
                                                    any open state → rejected / withdrawn

Rules:
- ``ready_for_review`` needs an approved resume version and the required checklist items.
- Submission is never automated. You approve submission, submit on the employer's site,
  then record **confirmation evidence**; only then does the status become ``applied``.
- The resume version is frozen once applied.
- Duplicate applications (same job, same apply URL, or same company+title recently)
  are blocked unless you record why it isn't a duplicate.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import (
    Application,
    ApplicationEvent,
    ApplicationEvidence,
    Job,
    ResumeVersion,
    utcnow,
)
from jobpilot.services import audit, profile as profile_svc, resumes as resume_svc, review, storage
from jobpilot.services.text_utils import normalize_company, normalize_title

TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"shortlisted", "preparing", "withdrawn"},
    "shortlisted": {"discovered", "preparing", "withdrawn"},
    "preparing": {"shortlisted", "ready_for_review", "withdrawn"},
    "ready_for_review": {"preparing", "applied", "withdrawn"},
    "applied": {"assessment", "interview", "rejected", "offer", "withdrawn"},
    "assessment": {"interview", "rejected", "offer", "withdrawn"},
    "interview": {"assessment", "offer", "rejected", "withdrawn"},
    "offer": {"withdrawn"},
    "rejected": {"applied"},  # reopen if the rejection was recorded by mistake
    "withdrawn": {"discovered"},
}
OPEN_STATES = {"discovered", "shortlisted", "preparing", "ready_for_review"}
SUBMITTED_STATES = {"applied", "assessment", "interview", "offer", "rejected"}
CLOSED_STATES = {"rejected", "withdrawn", "offer"}
CONFIRMATION_KINDS = {"confirmation_email", "confirmation_page", "portal_status", "screenshot"}
EVIDENCE_KINDS = CONFIRMATION_KINDS | {"file", "note"}
EVIDENCE_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".txt", ".eml", ".html")
DUPLICATE_WINDOW_DAYS = 120

CHECKLIST_ITEMS = [
    # key, label, required, auto
    ("review_job", "Read the full job description and eligibility warnings", True, False),
    ("resume", "Approved resume version selected", True, True),
    ("profile", "Profile details confirmed", True, True),
    ("apply_link", "Application page opens and is still accepting applications", True, False),
    ("answers", "Answers to application questions prepared (written by you)", True, False),
    ("cover_note", "Cover letter / note prepared, if the application asks for one", False, False),
    ("outreach", "Recruiter or referral outreach considered (optional)", False, False),
]


class ApplicationError(ValueError):
    pass


def find_duplicates(session: Session, job: Job, exclude_id: int | None = None) -> list[tuple[Application, str]]:
    out: list[tuple[Application, str]] = []
    stmt = select(Application).where(Application.status != "withdrawn")
    if exclude_id:
        stmt = stmt.where(Application.id != exclude_id)
    cutoff = utcnow() - timedelta(days=DUPLICATE_WINDOW_DAYS)
    company, title = normalize_company(job.company_name), normalize_title(job.title)
    for app in session.scalars(stmt):
        other = app.job
        if other.id == job.id:
            out.append((app, "same job"))
        elif job.apply_url and app.apply_url and app.apply_url == job.apply_url:
            out.append((app, "same application URL"))
        elif company and normalize_company(other.company_name) == company and normalize_title(other.title) == title \
                and app.created_at >= cutoff:
            out.append((app, f"same company and title within {DUPLICATE_WINDOW_DAYS} days"))
        elif job.repost_of_id and other.id == job.repost_of_id:
            out.append((app, "this job is a repost of one you already applied to"))
    return out


def create(session: Session, job: Job, status: str = "preparing", override_note: str | None = None) -> Application:
    if status not in ("discovered", "shortlisted", "preparing"):
        raise ApplicationError("New applications start as discovered, shortlisted or preparing")
    dups = find_duplicates(session, job)
    if any(reason == "same job" for _, reason in dups):
        raise ApplicationError(f"An application for this job already exists (#{dups[0][0].id})")
    if dups and not (override_note and override_note.strip()):
        details = "; ".join(f"#{a.id} ({r})" for a, r in dups)
        raise ApplicationError(f"Possible duplicate application: {details}. Add a note explaining why to continue.")
    app = Application(job_id=job.id, status=status, apply_url=job.apply_url or job.source_url,
                      duplicate_override_note=override_note if dups else None, checklist={})
    session.add(app)
    session.flush()
    session.add(ApplicationEvent(application_id=app.id, from_status=None, to_status=status, note="created"))
    if job.user_status == "new":
        job.user_status = "shortlisted"
    if status == "preparing":
        _prepare(session, app)
        prepare_snapshot(session, app)
    audit.record(session, "application.created", "application", app.id, job_id=job.id, status=status)
    return app


def _prepare(session: Session, app: Application) -> None:
    """Select the resume (if none chosen) and refresh the checklist."""
    if app.resume_version_id is None:
        sel = resume_svc.select_for_job(session, app.job)
        if sel["version"] is not None:
            app.resume_version_id = sel["version"].id
            app.resume_selection_reason = sel["reason"]
        else:
            review.open_item(session, "resume_missing", "application", app.id,
                             f"No suitable resume for {app.job.title}", sel["reason"])
    refresh_checklist(session, app)


def compute_checklist(session: Session, app: Application) -> dict[str, Any]:
    """Current checklist state (automatic items re-evaluated). Read-only."""
    current = dict(app.checklist or {})
    profile_ok = profile_svc.get_profile(session).confirmed_at is not None
    version = session.get(ResumeVersion, app.resume_version_id) if app.resume_version_id else None
    auto = {"resume": bool(version and version.status == "approved"), "profile": profile_ok}
    out = {}
    for key, label, required, is_auto in CHECKLIST_ITEMS:
        done = auto[key] if is_auto else bool((current.get(key) or {}).get("done"))
        out[key] = {"label": label, "required": required, "auto": is_auto, "done": done}
    return out


def prepare_snapshot(session: Session, app: Application) -> dict[str, Any]:
    """(Re)build the application's preparation record from current verified data and store it.

    References people and facts by id/label only; it does not copy your contact details.
    """
    from jobpilot.models import Contact, ContactJob, EmailDraft, Employment, Project
    from jobpilot.services import answers as answer_svc
    from jobpilot.services.jobs import canonical_url
    from jobpilot.services.skills_taxonomy import extract_skills

    job = app.job
    m = job.match_details or {}
    rel, elig, sk = m.get("relevance") or {}, m.get("eligibility") or {}, m.get("skills") or {}
    sel = resume_svc.select_for_job(session, job)
    version = session.get(ResumeVersion, app.resume_version_id) if app.resume_version_id else None
    job_skills = set(sk.get("mandatory") or []) | set(sk.get("preferred") or [])
    experience = []
    for e in session.scalars(select(Employment).where(Employment.status == "confirmed")):
        hits = sorted(job_skills & set(extract_skills(f"{e.title}\n{e.description or ''}")))
        if hits:
            kind = "work" + (f" ({e.role_type.replace('_', ' ')})" if e.role_type else " (kind of work not recorded)")
            experience.append({"kind": kind, "label": f"{e.title} at {e.company}", "skills": hits})
    for pr in session.scalars(select(Project).where(Project.status == "confirmed")):
        hits = sorted(job_skills & set(extract_skills(f"{pr.name}\n{pr.description or ''}\n{', '.join(pr.technologies or [])}")))
        if hits:
            kind = {"personal": "personal project", "academic": "academic project",
                    "learning_exercise": "learning exercise", "open_source": "open-source contribution"}.get(
                        pr.kind or "personal", "personal project")
            experience.append({"kind": kind + " (not work experience)", "label": pr.name, "skills": hits})
    claims = resume_svc.claim_check(session, version.extracted_text) if version else None
    if claims:
        claims = {"contradictions": claims["contradictions"], "metrics": len(claims["metrics"]),
                  "unconfirmed_job_skills": sorted(job_skills & set(claims["unconfirmed_skills"]))}
    contact_ids = set(session.scalars(select(ContactJob.contact_id).where(ContactJob.job_id == job.id)))
    contacts = [c for c in session.scalars(select(Contact).where(
        (Contact.id.in_(contact_ids)) | (Contact.job_id == job.id) |
        ((Contact.company_id == job.company_id) if job.company_id else False)))]
    qs = answer_svc.questions(session, app)
    snap = {
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "job": {"id": job.id, "title": job.title, "company": job.company_name, "location": job.location,
                "work_mode": job.remote_type, "employment_type": job.employment_type,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "first_seen_at": job.first_seen_at.isoformat() if job.first_seen_at else None,
                "sources": sorted({s.source.name for s in job.sightings if s.source}),
                "source_url": job.source_url, "status": job.status},
        "application_url": app.apply_url, "application_url_canonical": canonical_url(app.apply_url),
        "match": {"score": job.relevance_score, "recommendation": (m.get("recommendation") or {}).get("label"),
                  "eligibility": job.eligibility, "confidence": job.confidence,
                  "hard_conflicts": elig.get("hard_conflicts") or [],
                  "warnings": [f"{c['name']}: {c['explanation']}" for c in elig.get("checks", [])
                               if c.get("status") in ("warn", "fail", "unknown")],
                  "components": [{"name": c["name"], "points": c["points"], "max": c["max"],
                                  "explanation": c["explanation"]} for c in rel.get("components", [])]},
        "resume": {"version_id": version.id if version else None, "label": version.label if version else None,
                   "selected_by": app.resume_selection_reason, "suggested_version_id": sel["version"].id if sel["version"] else None,
                   "warnings": sel["warnings"], "missing": sel["missing"], "claims": claims},
        "deadline": job_deadline(job),
        "facts": {"skills_matched": sk.get("have") or [], "skills_unverified": sk.get("unverified") or [],
                  "skills_missing": sk.get("missing_mandatory") or [], "experience": experience},
        "missing": {"profile": m.get("missing_profile_info") or [], "job": m.get("missing_job_info") or [],
                    "questions_unanswered": sum(1 for x in qs if x["q"].status != "approved")},
        "answers": {"total": len(qs), "approved": sum(1 for x in qs if x["q"].status == "approved"),
                    "sensitive": sum(1 for x in qs if x["q"].sensitive),
                    "bank_suggestions": sum(1 for x in qs if x["suggestion"])},
        "contacts": [{"id": c.id, "name": c.display_name, "role": c.role_type, "email_status": c.email_status,
                      "source": c.source_type, "do_not_contact": c.do_not_contact} for c in contacts],
        "drafts": [{"id": d.id, "kind": d.kind, "status": d.status} for d in session.scalars(
            select(EmailDraft).where((EmailDraft.application_id == app.id) | (EmailDraft.job_id == job.id)))],
        "checklist": compute_checklist(session, app),
        "review_status": app.status,
    }
    snap["next_action"] = next_action(session, app, snap)
    app.preparation, app.prepared_at = snap, utcnow()
    audit.record(session, "application.prepared", "application", app.id)
    return snap


def job_deadline(job: Job) -> dict[str, Any] | None:
    """The posting's stated deadline, only from the source's structured field or an explicit sentence. Never guessed."""
    if job.valid_through:
        return {"date": job.valid_through.date().isoformat(), "basis": (job.raw or {}).get("deadline_source") or "source data"}
    ev = ((job.match_details or {}).get("requirements") or {}).get("evidence", {}).get("deadline") or {}
    if ev.get("basis") == "explicit" and ev.get("value"):
        return {"date": ev["value"], "basis": f"stated in the posting (“{(ev.get('snippet') or '')[:120]}”)"}
    return None


def next_action(session: Session, app: Application, snap: dict[str, Any] | None = None) -> str:
    """The single most useful next step for this application. Read-only."""
    from jobpilot.services import answers as answer_svc

    if app.status in OPEN_STATES and app.job.status != "active":
        return (f"The listing is {app.job.status}: check the posting is still open before continuing, or withdraw. "
                "(An expired listing is not a rejection.)")
    if app.status in ("discovered", "shortlisted"):
        return "Start preparing: this selects an approved resume and builds the preparation record."
    if app.status == "preparing":
        claims = ((snap or app.preparation or {}).get("resume") or {}).get("claims") or {}
        if app.resume_version_id is None:
            return "Choose or approve a resume version for this application."
        if claims.get("contradictions"):
            return "Check the resume: it describes work differently from your profile (see Preparation record)."
        missing = [i["label"] for i in compute_checklist(session, app).values() if i["required"] and not i["done"]]
        if missing:
            return "Complete the checklist: " + missing[0]
        open_q = answer_svc.unanswered_count(session, app)
        if open_q:
            return f"Answer and approve {open_q} application question(s)."
        return "Move to “ready for review”."
    if app.status == "ready_for_review":
        if app.submission_approved_at is None:
            return "Review the prepared application and approve submission."
        return ("Submit on the employer's site yourself, then add confirmation evidence and record the submission. "
                "Nothing is submitted for you.")
    if app.status in ("applied", "assessment", "interview"):
        return "Wait for the employer; record any reply, assessment or interview. Silence is not a rejection."
    return "No action needed."


def refresh_checklist(session: Session, app: Application) -> dict[str, Any]:
    """Recompute and persist the checklist; resolves the missing-resume review item once fixed."""
    out = compute_checklist(session, app)
    app.checklist = out
    if out["resume"]["done"]:
        review.resolve_items(session, "resume_missing", "application", app.id, "resume selected")
    return out


def set_check(session: Session, app: Application, key: str, done: bool) -> None:
    items = refresh_checklist(session, app)
    if key not in items or items[key]["auto"]:
        raise ApplicationError("Unknown or automatic checklist item")
    items = dict(items)
    items[key] = {**items[key], "done": done}
    app.checklist = items


def select_resume(session: Session, app: Application, version_id: int) -> None:
    if app.status in SUBMITTED_STATES:
        raise ApplicationError("The resume version is frozen after submission")
    v = session.get(ResumeVersion, version_id)
    if v is None or v.status != "approved":
        raise ApplicationError("Only approved resume versions can be used")
    app.resume_version_id = v.id
    app.resume_selection_reason = "chosen manually"
    refresh_checklist(session, app)
    audit.record(session, "application.resume_selected", "application", app.id, resume_version_id=v.id)


def _event(session: Session, app: Application, to: str, note: str | None, actor: str = "user") -> None:
    session.add(ApplicationEvent(application_id=app.id, from_status=app.status, to_status=to, note=note, actor=actor))
    audit.record(session, "application.status", "application", app.id, actor=actor, frm=app.status, to=to)
    app.status = to


def transition(session: Session, app: Application, to: str, note: str | None = None) -> Application:
    if to not in TRANSITIONS.get(app.status, set()):
        raise ApplicationError(f"Cannot move from {app.status} to {to}")
    if to == "applied":
        raise ApplicationError("Use 'record submission' with confirmation evidence to mark as applied")
    if to == "ready_for_review":
        from jobpilot.services import answers as answer_svc

        items = refresh_checklist(session, app)
        missing = [i["label"] for i in items.values() if i["required"] and not i["done"]]
        if missing:
            raise ApplicationError("Complete the checklist first: " + "; ".join(missing))
        open_q = answer_svc.unanswered_count(session, app)
        if open_q:
            raise ApplicationError(f"{open_q} application question(s) still need an approved answer")
    if to == "preparing":
        _prepare(session, app)
    _event(session, app, to, note)
    if to in ("preparing", "ready_for_review"):
        prepare_snapshot(session, app)
    if to == "ready_for_review":
        review.open_item(session, "submission_approval", "application", app.id,
                         f"Approve submission: {app.job.title} at {app.job.company_name or '?'}",
                         "Review the prepared application and approve submission.")
    if to in CLOSED_STATES:
        app.closed_at = utcnow()
        _suppress_followups(session, app, f"application {to}")
    if to in ("withdrawn", "preparing", "shortlisted", "discovered"):
        app.submission_approved_at = None
        review.resolve_items(session, "submission_approval", "application", app.id, f"moved to {to}")
    return app


def _suppress_followups(session: Session, app: Application, reason: str) -> None:
    from jobpilot.models import EmailDraft

    for d in session.scalars(select(EmailDraft).where(
            (EmailDraft.application_id == app.id) | (EmailDraft.job_id == app.job_id),
            EmailDraft.follow_up_due_at.is_not(None))):
        d.follow_up_due_at = None
        d.suppressed_reason = reason


def approve_submission(session: Session, app: Application) -> None:
    """Your explicit go-ahead to submit (you then submit on the employer's site)."""
    if app.status != "ready_for_review":
        raise ApplicationError("Only applications that are ready for review can be approved for submission")
    app.submission_approved_at = utcnow()
    review.resolve_items(session, "submission_approval", "application", app.id, "submission approved")
    audit.record(session, "application.submission_approved", "application", app.id)


def add_evidence(session: Session, settings: Settings, app: Application, kind: str, text: str | None = None,
                 filename: str | None = None, data: bytes | None = None) -> ApplicationEvidence:
    if kind not in EVIDENCE_KINDS:
        raise ApplicationError("Unknown evidence type")
    text = (text or "").strip() or None
    if not text and not data:
        raise ApplicationError("Evidence needs text or a file")
    ev = ApplicationEvidence(application_id=app.id, kind=kind, text=text[:20000] if text else None)
    if data:
        stored = storage.store(settings, "evidence", filename or "evidence", data, allowed=EVIDENCE_EXTS)
        ev.file_path, ev.original_filename, ev.sha256 = stored.rel_path, stored.original_name, stored.sha256
    session.add(ev)
    session.flush()
    audit.record(session, "application.evidence_added", "application", app.id, kind=kind)
    return ev


def record_submission(session: Session, app: Application, method: str, note: str, attest: bool,
                      submitted_at=None) -> Application:
    """Mark an application as applied — only with confirmation evidence and your attestation."""
    if app.status != "ready_for_review":
        raise ApplicationError("Prepare the application and move it to 'ready for review' first")
    if app.submission_approved_at is None:
        raise ApplicationError("Approve the submission first")
    if not attest:
        raise ApplicationError("Confirm that you actually submitted the application")
    if method not in CONFIRMATION_KINDS:
        raise ApplicationError("Choose how submission was confirmed")
    if not any(e.kind in CONFIRMATION_KINDS for e in app.evidence):
        raise ApplicationError("Add confirmation evidence (email text, confirmation page, screenshot or portal status) first")
    if app.resume_version_id is None:
        raise ApplicationError("No resume version recorded for this application")
    app.applied_at = submitted_at or utcnow()
    app.confirmation_method = method
    app.confirmation_note = (note or "").strip() or None
    _event(session, app, "applied", f"submission confirmed via {method}")
    if app.job.user_status != "shortlisted":
        app.job.user_status = "shortlisted"
    audit.record(session, "application.submitted", "application", app.id, method=method,
                 resume_version_id=app.resume_version_id)
    return app


def list_by_status(session: Session) -> dict[str, list[Application]]:
    from jobpilot.models.applications import APPLICATION_STATUSES

    out: dict[str, list[Application]] = {s: [] for s in APPLICATION_STATUSES}
    for app in session.scalars(select(Application).order_by(Application.updated_at.desc())):
        out[app.status].append(app)
    return out


def for_job(session: Session, job_id: int) -> Application | None:
    return session.scalar(select(Application).where(Application.job_id == job_id).order_by(Application.id.desc()))


def counts(session: Session) -> dict[str, int]:
    return dict(session.execute(select(Application.status, func.count()).group_by(Application.status)).all())
