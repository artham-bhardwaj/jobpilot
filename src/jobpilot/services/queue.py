"""Central review queue: everything waiting for a human decision, with reasons and safe actions.

Items are computed from current state (jobs, resumes, contacts, drafts, runs) plus stored review items.
Acknowledging a computed item stores a resolved ``ReviewItem`` (kind ``ack:<category>``) so it stays
dismissed. Bulk actions are limited to non-consequential steps (mark reviewed, shortlist, dismiss a job,
acknowledge a notice). Approving drafts, approving submissions and sending anything are never bulk actions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobpilot.models import (
    Application,
    BrowserRun,
    Contact,
    ContactJob,
    EmailDraft,
    Job,
    JobSource,
    ReviewItem,
    SourceRun,
    TaskRun,
    utcnow,
)
from jobpilot.services import audit, settings_store

CATEGORIES = {
    "strong_match": ("Strong matches", 90),
    "browser_intervention": ("Browser needs you", 85),
    "submission_approval": ("Submission approval", 80),
    "deadline_soon": ("Deadlines approaching", 78),
    "interview_detected": ("Possible interviews", 75),
    "failed_operation": ("Failed or incomplete operations", 70),
    "eligibility_uncertain": ("Eligibility uncertain", 60),
    "draft_review": ("Email drafts to review", 55),
    "follow_up_due": ("Follow-ups due", 50),
    "application_check": ("Applications to check on", 48),
    "no_suitable_resume": ("No suitable resume", 45),
    "contact_verification": ("Contacts to verify", 40),
    "missing_profile_info": ("Missing profile information", 35),
    "new_job": ("New jobs to review", 30),
}
STORED_KIND_TO_CATEGORY = {
    "browser_paused": "browser_intervention", "submission_approval": "submission_approval",
    "interview_detected": "interview_detected", "follow_up_due": "follow_up_due", "resume_missing": "no_suitable_resume",
    "deadline_soon": "deadline_soon", "application_stale": "application_check", "listing_expired": "application_check",
}
JOB_CATEGORIES = {"strong_match", "eligibility_uncertain", "new_job"}
BULK_ACTIONS = {
    "mark_reviewed": JOB_CATEGORIES,
    "shortlist": JOB_CATEGORIES,
    "dismiss_job": JOB_CATEGORIES,
    "acknowledge": {"failed_operation", "missing_profile_info", "no_suitable_resume", "contact_verification"},
}
FAILED_LOOKBACK_DAYS = 7


def stable_id(text: str) -> int:
    """Process-independent id for computed items (Python's hash() is randomised per run)."""
    import hashlib

    return int(hashlib.sha256(text.encode()).hexdigest()[:7], 16)


def _item(category: str, key: str, title: str, why: str, *, job: Job | None = None, state: str = "",
          recommended: str = "", actions: list[dict] | None = None, created=None, score: float | None = None,
          link: str | None = None) -> dict[str, Any]:
    label, priority = CATEGORIES[category]
    return {"key": key, "category": category, "category_label": label, "priority": priority, "title": title,
            "why": why, "job_id": job.id if job else None, "job_title": job.title if job else None,
            "company": (job.company_name if job else None), "state": state, "recommended": recommended,
            "actions": actions or [], "created_at": created or utcnow(), "score": score, "link": link,
            "bulk": sorted(a for a, cats in BULK_ACTIONS.items() if category in cats)}


def _acknowledged(session: Session) -> set[tuple[str, str, int]]:
    rows = session.execute(select(ReviewItem.kind, ReviewItem.entity_type, ReviewItem.entity_id)
                           .where(ReviewItem.status != "open", or_(ReviewItem.kind.like("ack:%"),
                                                                   ReviewItem.kind == "job_review")))
    return {(k, t, i) for k, t, i in rows}


def _job_actions(job: Job) -> list[dict]:
    return [{"label": "Start application", "url": f"/jobs/{job.id}/application", "method": "post", "consequential": False},
            {"label": "Shortlist", "url": f"/review/job/{job.id}", "method": "post", "data": {"action": "shortlist"}},
            {"label": "Dismiss", "url": f"/review/job/{job.id}", "method": "post", "data": {"action": "dismiss"}},
            {"label": "Mark reviewed", "url": f"/review/job/{job.id}", "method": "post", "data": {"action": "reviewed"}}]


def build(session: Session) -> list[dict[str, Any]]:
    from jobpilot.services.resumes import select_for_job

    acked = _acknowledged(session)
    threshold = settings_store.get(session, "matching")["relevance_threshold"]
    items: list[dict[str, Any]] = []
    applied_jobs = set(session.scalars(select(Application.job_id)))
    active = list(session.scalars(select(Job).where(Job.status == "active", Job.match_details.is_not(None))))

    missing_profile: dict[str, list[Job]] = {}
    for job in active:
        m = job.match_details or {}
        rec = (m.get("recommendation") or {}).get("key")
        reviewed = ("job_review", "job", job.id) in acked
        relevant = (job.relevance_score or 0) >= threshold and not job.hard_conflict
        if job.user_status != "dismissed" and job.id not in applied_jobs:
            for info in m.get("missing_profile_info") or []:
                if relevant:
                    missing_profile.setdefault(info, []).append(job)
        if job.user_status != "new" or job.id in applied_jobs or reviewed:
            continue
        reasons = "; ".join(m.get("review_reasons") or [])
        state = f"score {job.relevance_score:g} · {job.eligibility} · {job.confidence or '?'} confidence"
        if rec == "strong":
            items.append(_item("strong_match", f"job:{job.id}", job.title, reasons or "strong match", job=job, state=state,
                               recommended="Read the match explanation, then start an application or dismiss",
                               actions=_job_actions(job), created=job.first_seen_at, score=job.relevance_score,
                               link=f"/jobs/{job.id}"))
        elif job.eligibility == "unknown" and relevant:
            items.append(_item("eligibility_uncertain", f"job:{job.id}", job.title,
                               reasons or "relevant, but eligibility could not be determined", job=job, state=state,
                               recommended="Check the posting for the unknown requirements (and fill gaps in your profile)",
                               actions=_job_actions(job), created=job.first_seen_at, score=job.relevance_score,
                               link=f"/jobs/{job.id}"))
        elif m.get("needs_review"):
            items.append(_item("new_job", f"job:{job.id}", job.title, reasons, job=job, state=state,
                               recommended="Triage: shortlist, dismiss or mark reviewed", actions=_job_actions(job),
                               created=job.first_seen_at, score=job.relevance_score, link=f"/jobs/{job.id}"))
        if relevant and rec in ("strong", "relevant") and ("ack:no_suitable_resume", "job", job.id) not in acked:
            sel = select_for_job(session, job)
            if sel["missing"] or sel["unsuitable"]:
                items.append(_item("no_suitable_resume", f"resume:{job.id}", job.title,
                                   sel["reason"] if sel["missing"] else "; ".join(sel["warnings"][:2]), job=job,
                                   state="no approved resume" if sel["missing"] else "only an unsuitable fallback",
                                   recommended="Import and approve a resume for this role category",
                                   actions=[{"label": "Resume library", "url": "/resumes", "method": "get"},
                                            {"label": "Selection review", "url": f"/jobs/{job.id}/resume-review", "method": "get"}],
                                   created=job.first_seen_at, score=job.relevance_score, link=f"/jobs/{job.id}/resume-review"))

    for idx, (info, jobs) in enumerate(sorted(missing_profile.items())):
        ack_id = stable_id(info)
        if ("ack:missing_profile_info", "profile", ack_id) in acked:
            continue
        items.append(_item("missing_profile_info", f"profile:{ack_id}", f"Profile: {info}",
                           f"needed to assess {len(jobs)} relevant job(s), e.g. {jobs[0].title}",
                           state="not in your profile", recommended="Add it on the Profile page if you can state it accurately",
                           actions=[{"label": "Open profile", "url": "/profile", "method": "get"}],
                           link="/profile"))

    from jobpilot.services.hub import open_conflicts

    conflicts = open_conflicts(session)
    if conflicts:
        ack_id = stable_id("hub:" + "|".join(conflicts))
        if ("ack:missing_profile_info", "profile", ack_id) not in acked:
            items.append(_item("missing_profile_info", f"profile:{ack_id}", "Profile: resume and profile disagree",
                               f"{len(conflicts)} value(s) differ, e.g. {conflicts[0]}", state="conflicting information",
                               recommended="Open the information hub and decide which value is right",
                               actions=[{"label": "Open hub", "url": "/hub#attention", "method": "get"}],
                               link="/hub#attention"))

    active_ids = {j.id for j in active}
    linked = set(session.scalars(select(ContactJob.contact_id).where(ContactJob.job_id.in_(active_ids)))) if active_ids else set()
    for c in session.scalars(select(Contact).where(Contact.do_not_contact.is_(False),
                                                   Contact.email_status.in_(("unverified", "guessed")))):
        if not (c.id in linked or (c.job_id in active_ids)):
            continue
        if ("ack:contact_verification", "contact", c.id) in acked:
            continue
        items.append(_item("contact_verification", f"contact:{c.id}", c.display_name,
                           f"{c.email} is {c.email_status} (source: {c.source_type})", state=c.email_status,
                           recommended="Verify the address from an authoritative source before relying on it",
                           actions=[{"label": "Open contact", "url": f"/contacts/{c.id}", "method": "get"}],
                           created=c.discovered_at, link=f"/contacts/{c.id}"))

    for d in session.scalars(select(EmailDraft).where(EmailDraft.status == "draft")):
        why = "fill in missing facts: " + ", ".join(d.missing_fields) if d.missing_fields else "ready for your review"
        items.append(_item("draft_review", f"draft:{d.id}", d.subject, why, job=d.job, state="draft",
                           recommended="Review, edit and approve (or cancel)",
                           actions=[{"label": "Open draft", "url": f"/drafts/{d.id}", "method": "get"}],
                           created=d.updated_at, link=f"/drafts/{d.id}"))

    for ri in session.scalars(select(ReviewItem).where(ReviewItem.status == "open", ReviewItem.kind.in_(list(STORED_KIND_TO_CATEGORY)))):
        cat = STORED_KIND_TO_CATEGORY[ri.kind]
        link = f"/applications/{ri.entity_id}" if ri.entity_type == "application" else (
            f"/drafts/{ri.entity_id}" if ri.entity_type == "draft" else None)
        if ri.kind == "interview_detected":
            link = f"/interviews?suggestion={ri.id}"
        app = session.get(Application, ri.entity_id) if ri.entity_type == "application" else None
        job = app.job if app else (session.get(Job, ri.entity_id) if ri.entity_type == "job" else None)
        if ri.entity_type == "job":
            existing = session.scalar(select(Application).where(Application.job_id == ri.entity_id).limit(1))
            link = f"/applications/{existing.id}" if existing else f"/jobs/{ri.entity_id}"
        items.append(_item(cat, f"stored:{ri.id}", ri.title, ri.reason or "", job=job,
                           state=ri.kind.replace("_", " "),
                           recommended={"browser_intervention": "Finish the paused steps yourself in the browser",
                                        "submission_approval": "Review the prepared application, then approve submission",
                                        "interview_detected": "Check the extracted details and confirm or dismiss",
                                        "follow_up_due": "Draft a follow-up or record that they replied",
                                        "no_suitable_resume": "Import and approve a suitable resume",
                                        "deadline_soon": "Finish and submit before the deadline, or dismiss if not applying",
                                        "application_check": "Check the employer's portal; record any update, "
                                                             "follow up politely, or withdraw. Never assume a rejection"}[cat],
                           actions=[{"label": "Open", "url": link, "method": "get"}] if link else [],
                           created=ri.created_at, link=link) | {"stored_id": ri.id})

    since = utcnow() - timedelta(days=FAILED_LOOKBACK_DAYS)
    for run in session.scalars(select(TaskRun).where(TaskRun.status == "failed", TaskRun.started_at >= since)):
        if ("ack:failed_operation", "task_run", run.id) in acked:
            continue
        items.append(_item("failed_operation", f"task:{run.id}", f"Task '{run.task_name}' failed", run.message or "",
                           state="failed", recommended="Read the error, fix the cause, then run it again from System",
                           actions=[{"label": "System", "url": "/system#tasks", "method": "get"}],
                           created=run.started_at, link="/system#tasks"))
    for src in session.scalars(select(JobSource)):
        last = session.scalar(select(SourceRun).where(SourceRun.source_id == src.id, SourceRun.dry_run.is_(False))
                              .order_by(SourceRun.id.desc()).limit(1))
        if last and last.status in ("failed", "unavailable", "anomaly", "partial") and \
                ("ack:failed_operation", "source_run", last.id) not in acked:
            items.append(_item("failed_operation", f"source_run:{last.id}", f"Source '{src.name}': {last.status}",
                               (last.warnings or [None])[0] or (last.error or "")[:300], state=last.status,
                               recommended="Check the source's run history; nothing was expired by this run",
                               actions=[{"label": "Source", "url": f"/sources/{src.id}", "method": "get"}],
                               created=last.started_at, link=f"/sources/{src.id}"))
    for br in session.scalars(select(BrowserRun).where(BrowserRun.status == "failed", BrowserRun.started_at >= since)):
        if ("ack:failed_operation", "browser_run", br.id) in acked:
            continue
        items.append(_item("failed_operation", f"browser_run:{br.id}", f"Browser assistance failed (application #{br.application_id})",
                           br.message or "", job=br.application.job, state="failed",
                           recommended="Use the manual workflow on the application page",
                           actions=[{"label": "Application", "url": f"/applications/{br.application_id}", "method": "get"}],
                           created=br.started_at, link=f"/applications/{br.application_id}"))
    return items


def query(session: Session, *, category: str | None = None, q: str | None = None, sort: str = "priority",
          page: int = 1, per_page: int = 25) -> dict[str, Any]:
    items = build(session)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
    if category:
        items = [i for i in items if i["category"] == category]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in " ".join(str(x or "") for x in (i["title"], i["why"], i["company"], i["job_title"])).lower()]
    keyf = {"priority": lambda i: (-i["priority"], -(i["score"] or 0)),
            "newest": lambda i: -i["created_at"].timestamp(),
            "score": lambda i: -(i["score"] or -1)}.get(sort)
    items.sort(key=keyf or (lambda i: (-i["priority"], -(i["score"] or 0))))
    total = len(items)
    page = max(1, page)
    start = (page - 1) * per_page
    return {"rows": items[start:start + per_page], "total": total, "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page), "counts": counts}


def count(session: Session) -> int:
    return len(build(session))


def _ack(session: Session, category: str, entity_type: str, entity_id: int, title: str, note: str) -> None:
    session.add(ReviewItem(kind=f"ack:{category}", entity_type=entity_type, entity_id=entity_id, title=title[:300],
                           status="resolved", resolved_at=utcnow(), resolution=note))


def apply_action(session: Session, item: dict[str, Any], action: str, *, bulk: bool = False) -> None:
    """Apply one queue action after validating it is allowed for this item."""
    from jobpilot.services import review
    from jobpilot.services.jobs import set_user_status

    if action not in BULK_ACTIONS or item["category"] not in BULK_ACTIONS[action]:
        raise ValueError(f"'{action}' is not allowed for {item['category_label']}")
    kind, _, ident = item["key"].partition(":")
    if action in ("mark_reviewed", "shortlist", "dismiss_job"):
        job = session.get(Job, item["job_id"])
        if action == "shortlist":
            set_user_status(session, job, "shortlisted")
        elif action == "dismiss_job":
            set_user_status(session, job, "dismissed")
        review.mark_job_reviewed(session, job, f"{action}{' (bulk)' if bulk else ''}")
        return
    entity = {"task": "task_run", "source_run": "source_run", "browser_run": "browser_run", "contact": "contact",
              "resume": "job", "profile": "profile"}[kind]
    _ack(session, item["category"], entity, int(ident), item["title"], "acknowledged" + (" (bulk)" if bulk else ""))
    audit.record(session, "queue.acknowledged", entity, int(ident), category=item["category"], bulk=bulk)


def bulk_apply(session: Session, keys: list[str], action: str) -> dict[str, Any]:
    by_key = {i["key"]: i for i in build(session)}
    done, refused = [], []
    for key in dict.fromkeys(keys):
        item = by_key.get(key)
        if item is None:
            refused.append((key, "no longer in the queue"))
            continue
        try:
            apply_action(session, item, action, bulk=True)
            done.append(key)
        except ValueError as exc:
            refused.append((key, str(exc)))
    audit.record(session, "queue.bulk", None, None, bulk_action=action, applied=len(done), refused=len(refused))
    return {"applied": done, "refused": refused}
