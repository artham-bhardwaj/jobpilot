"""Job explorer, job detail, manual/URL/file job entry and source configuration."""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Job, JobSource
from jobpilot.services import jobs as job_svc
from jobpilot.services import matching, resumes as rs, settings_store
from jobpilot.services.tasks import run_task
from jobpilot.sources import ADAPTERS
from jobpilot.sources.http import FetchError, PoliteHttp
from jobpilot.web.deps import first_error, form_dict, get_db, get_or_404, redirect, render

router = APIRouter()
PAGE_SIZE = 50


def _int(v, default=None):
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


@router.get("/jobs")
def explorer(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    filters = {
        "q": qp.get("q") or None,
        "status": qp.get("status", "active") or None,
        "category": qp.get("category") or None,
        "eligibility": qp.get("eligibility") or None,
        "remote": qp.get("remote") or None,
        "user_status": qp.get("user_status") or None,
        "min_score": _int(qp.get("min_score")),
        "source_id": _int(qp.get("source_id")),
        "posted_within_days": _int(qp.get("posted_within_days")),
        "sort": qp.get("sort", "relevance"),
    }
    page = max(1, _int(qp.get("page"), 1))
    jobs, total = job_svc.search(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **filters)
    sources = list(db.scalars(select(JobSource).order_by(JobSource.name)))
    base_qs = "&".join(f"{k}={v}" for k, v in qp.items() if k != "page")
    return render(request, "jobs.html", jobs=jobs, total=total, page=page, pages=(total + PAGE_SIZE - 1) // PAGE_SIZE,
                  filters=filters, categories=settings_store.get(db, "role_categories"), sources=sources,
                  base_qs=base_qs, threshold=settings_store.get(db, "matching")["relevance_threshold"])


@router.get("/jobs/new")
def new_job_page(request: Request):
    return render(request, "job_new.html")


@router.post("/jobs/manual")
async def add_manual(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        job, outcome = job_svc.add_manual(db, job_svc.ManualJobIn.model_validate(form))
    except ValidationError as exc:
        return redirect("/jobs/new", err=first_error(exc))
    note = {"new": "Job added", "repost": "Added (looks like a repost of an expired job)"}.get(outcome, f"Matched an existing job ({outcome})")
    return redirect(f"/jobs/{job.id}", msg=note)


@router.post("/jobs/url")
async def add_url(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    url = form.get("url") or ""
    http = PoliteHttp(request.app.state.settings)
    try:
        job, outcome, warnings = job_svc.add_from_url(db, http, url, form.get("company_name"))
    except (FetchError, ValueError) as exc:
        return redirect("/jobs/new", err=f"Could not import from URL: {exc}")
    msg = f"Imported ({outcome})" + (f". Warnings: {'; '.join(warnings)}" if warnings else "")
    return redirect(f"/jobs/{job.id}", msg=msg)


@router.post("/jobs/import")
async def import_file(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        return redirect("/jobs/new", err="File too large")
    try:
        stats = job_svc.import_file(db, file.filename or "", data)
    except (ValueError, json.JSONDecodeError) as exc:
        return redirect("/jobs/new", err=f"Import failed: {exc}")
    msg = f"Imported: {stats['new']} new, {stats['updated']} updated, {stats['unchanged']} unchanged, {stats['repost']} reposts"
    if stats["errors"]:
        msg += f". {len(stats['errors'])} row(s) skipped: " + "; ".join(stats["errors"][:5])
    return redirect("/jobs?sort=newest", msg=msg)


@router.post("/jobs/rematch")
def rematch(db: Session = Depends(get_db)):
    n = matching.rematch_all(db)
    return redirect("/jobs", msg=f"Re-scored {n} active job(s) with current profile and settings")


@router.get("/jobs/{job_id:int}")
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)  # read-only: unscored jobs show a "Re-score" prompt instead
    selection = rs.select_for_job(db, job)
    extra = _job_extras(db, job)
    return render(request, "job_detail.html", job=job, m=job.match_details or {}, selection=selection,
                  categories=settings_store.get(db, "role_categories"), **extra)


def _job_extras(db: Session, job: Job) -> dict:
    """Related records from later stages (applications, contacts) if available."""
    from jobpilot.services import job_extras

    return job_extras.for_job(db, job)


@router.post("/jobs/{job_id:int}/status")
async def job_status(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    try:
        job_svc.set_user_status(db, job, form.get("status") or "")
    except ValueError as exc:
        return redirect(f"/jobs/{job_id}", err=str(exc))
    return redirect(form.get("next") or f"/jobs/{job_id}", msg=f"Marked {job.user_status}")


@router.post("/jobs/{job_id:int}/notes")
async def job_notes(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    job.notes = (form.get("notes") or "")[:10000] or None
    return redirect(f"/jobs/{job_id}", msg="Notes saved")


@router.post("/jobs/{job_id:int}/rematch")
def job_rematch(job_id: int, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    matching.match_job(db, job)
    return redirect(f"/jobs/{job_id}", msg="Re-scored")


@router.post("/jobs/{job_id:int}/check")
def job_check(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    result = job_svc.check_alive(db, PoliteHttp(request.app.state.settings), job)
    return redirect(f"/jobs/{job_id}", msg=f"Checked: {result}")


@router.post("/jobs/{job_id:int}/expire")
def job_expire(job_id: int, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    job_svc.mark_expired(db, job, reason="marked manually")
    return redirect(f"/jobs/{job_id}", msg="Marked expired")


# ---- Sources -----------------------------------------------------------------

@router.get("/sources")
def sources_page(request: Request, edit: int | None = None, db: Session = Depends(get_db)):
    sources = list(db.scalars(select(JobSource).order_by(JobSource.name)))
    editing = db.get(JobSource, edit) if edit else None
    return render(request, "sources.html", sources=sources, adapters=ADAPTERS, editing=editing)


@router.post("/sources")
@router.post("/sources/{source_id:int}")
async def source_save(request: Request, source_id: int | None = None, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        config = json.loads(form.get("config") or "{}")
        if not isinstance(config, dict):
            raise ValueError("config must be a JSON object")
        job_svc.save_source(db, job_svc.SourceIn(
            name=form.get("name") or "", kind=form.get("kind") or "", company_name=form.get("company_name"),
            config=config, enabled=bool(form.get("enabled")), terms_note=form.get("terms_note")), source_id)
    except (ValidationError, ValueError, LookupError, json.JSONDecodeError) as exc:
        return redirect("/sources", err=first_error(exc))
    return redirect("/sources", msg="Source saved")


@router.post("/sources/{source_id:int}/delete")
def source_delete(source_id: int, db: Session = Depends(get_db)):
    src = get_or_404(db, JobSource, source_id)
    if src is not None:
        db.delete(src)
    return redirect("/sources", msg="Source removed (its jobs are kept)")


@router.post("/sources/run")
async def sources_run(request: Request, background: BackgroundTasks):
    form = await form_dict(request)
    ids = [int(form["source_id"])] if form.get("source_id", "").isdigit() else None
    settings, factory = request.app.state.settings, request.app.state.sessionmaker

    def _job():
        run_task(settings, factory, "collect_jobs",
                 lambda s: job_svc.collect_all(s, settings, source_ids=ids), trigger="manual")

    background.add_task(_job)
    return redirect("/sources", msg="Collection started in the background — see System → Task runs for results")
