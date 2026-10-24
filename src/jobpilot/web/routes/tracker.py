"""Your application tracker: every job JobPilot found with its apply link, what you applied to (and with which
resume), applications by company with cooldowns, and dream companies first."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Application, Job, ResumeVersion, ReviewItem
from jobpilot.services import applications as apps
from jobpilot.services import tracker
from jobpilot.services.text_utils import normalize_company
from jobpilot.web.deps import form_dict, get_db, get_or_404, redirect, render

router = APIRouter()


@router.get("/tracker")
def tracker_page(request: Request, db: Session = Depends(get_db)):
    q = (request.query_params.get("q") or "").strip()[:80]
    show_all = request.query_params.get("all") == "1"
    jobs = tracker.relevant_jobs(db, include_ineligible=show_all)
    if q:
        jobs = [j for j in jobs if normalize_company(q) in normalize_company(j.company_name)
                or q.lower() in (j.title or "").lower()]
    app_by_job = {a.job_id: a for a in db.scalars(select(Application))}
    resumes = list(db.scalars(select(ResumeVersion).where(ResumeVersion.status == "approved")
                              .order_by(ResumeVersion.id.desc())))
    asks = list(db.scalars(select(ReviewItem).where(ReviewItem.kind == "email_outcome", ReviewItem.status == "open")))
    return render(request, "tracker.html", q=q, show_all=show_all, jobs=jobs, app_by_job=app_by_job,
                  groups=tracker.by_company(db, q or None), resumes=resumes, asks=asks,
                  dream=tracker.cfg(db)["dream_companies"], is_dream=lambda c: tracker.is_dream(db, c),
                  resume_label=tracker.resume_label, today=date.today().isoformat())


def _back(form: dict) -> str:
    """Where to return after a tracker action: the tracker, a Jobs page or the Overview (local paths only)."""
    nxt = form.get("next") or "/tracker"
    if nxt == "/" or (not nxt.startswith("//") and nxt.startswith(("/tracker", "/jobs"))):
        return nxt
    return "/tracker"


@router.post("/tracker/jobs/{job_id:int}/applied")
async def applied(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    note = form.get("resume_note")
    if form.get("resume_version_id") == "tailored":
        from jobpilot.services import tailored

        tr = tailored.latest(db, job.id)
        if tr is None:
            return redirect(_back(form), err="There is no tailored resume for this job yet")
        form["resume_version_id"] = ""
        note = f"tailored resume for this job (#{tr.id}, from resume version #{tr.source_version_id})"
    try:
        vid = int(form["resume_version_id"]) if form.get("resume_version_id") else None
        on = date.fromisoformat(form["applied_on"]) if form.get("applied_on") else None
        if on and on > date.today():
            raise apps.ApplicationError("The date you applied can't be in the future")
        app = tracker.record_self_applied(db, job, vid, resume_note=note, applied_on=on,
                                          via="dashboard")
    except (apps.ApplicationError, ValueError) as exc:
        return redirect(_back(form), err=str(exc))
    db.commit()
    return redirect(_back(form), msg=f"Recorded: you applied to {job.title} at {job.company_name} (#{app.id})")


@router.post("/tracker/jobs/{job_id:int}/later")
async def later(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    if form.get("action") == "not_interested":
        tracker.answer_not_interested(db, job)
        msg = f"Dismissed: {job.title}"
    else:
        tracker.answer_not_yet(db, job)
        msg = f"OK — you'll be asked again about {job.title} later"
    db.commit()
    return redirect(_back(form), msg=msg)


@router.post("/tracker/cooldown")
async def cooldown(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    company = (form.get("company") or "").strip()[:80]
    try:
        days = int(form.get("days") or 0)
        if not company:
            raise ValueError("Enter a company")
        tracker.set_cooldown(db, company, days or None)
    except ValueError as exc:
        return redirect(_back(form), err=str(exc))
    db.commit()
    return redirect(_back(form), msg=f"Cooldown for {company}: {days} days" if days else f"Cooldown for {company} removed")


@router.post("/tracker/emails/{item_id:int}")
async def email_outcome(item_id: int, request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    msg = tracker.decide_email_outcome(db, item_id, form.get("status") or "none")
    db.commit()
    return redirect(_back(form), msg=msg)


@router.post("/tracker/applications/{app_id:int}/undo")
async def undo(app_id: int, request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    msg = tracker.undo_outcome(db, get_or_404(db, Application, app_id))
    db.commit()
    return redirect(_back(form), msg=msg)
