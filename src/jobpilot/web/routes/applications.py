"""Application pipeline, preparation, submission evidence, browser assist and the review queue."""

from __future__ import annotations

import subprocess
import sys

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.automation.apply import manual_fields, site_for
from jobpilot.models import Application, ApplicationEvidence, Job, Resume, ReviewItem
from jobpilot.models.applications import APPLICATION_STATUSES
from jobpilot.services import applications as apps
from jobpilot.services import resumes as rs
from jobpilot.services import review, storage
from jobpilot.web.deps import form_dict, get_db, get_or_404, redirect, render

router = APIRouter()


@router.get("/applications")
def pipeline(request: Request, db: Session = Depends(get_db)):
    return render(request, "applications.html", by_status=apps.list_by_status(db), statuses=APPLICATION_STATUSES)


@router.post("/jobs/{job_id:int}/application")
async def create_application(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    try:
        app = apps.create(db, job, status=form.get("status") or "preparing", override_note=form.get("override_note"))
    except apps.ApplicationError as exc:
        return redirect(f"/jobs/{job_id}#application", err=str(exc))
    return redirect(f"/applications/{app.id}", msg="Application created")


@router.get("/applications/{app_id:int}")
def detail(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    checklist = apps.compute_checklist(db, app) if app.status in apps.OPEN_STATES else (app.checklist or {})
    approved = [(r, v) for r in rs.list_resumes(db) for v in r.versions if v.status == "approved"]
    settings = request.app.state.settings
    return render(request, "application_detail.html", app=app, job=app.job, checklist=checklist,
                  next_states=sorted(apps.TRANSITIONS.get(app.status, set()) - {"applied"}),
                  approved_versions=approved, duplicates=apps.find_duplicates(db, app.job, exclude_id=app.id),
                  manual=manual_fields(db), site=site_for(app.apply_url),
                  browser_enabled=settings.browser_enabled, confirmation_kinds=sorted(apps.CONFIRMATION_KINDS),
                  evidence_kinds=sorted(apps.EVIDENCE_KINDS))


@router.post("/applications/{app_id:int}/status")
async def status(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    try:
        apps.transition(db, app, form.get("to") or "", form.get("note"))
    except apps.ApplicationError as exc:
        return redirect(f"/applications/{app_id}", err=str(exc))
    return redirect(f"/applications/{app_id}", msg=f"Moved to {app.status}")


@router.post("/applications/{app_id:int}/check")
async def check(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    try:
        apps.set_check(db, app, form.get("key") or "", form.get("done") == "1")
    except apps.ApplicationError as exc:
        return redirect(f"/applications/{app_id}", err=str(exc))
    return redirect(f"/applications/{app_id}#checklist")


@router.post("/applications/{app_id:int}/resume")
async def choose_resume(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    try:
        apps.select_resume(db, app, int(form.get("version_id") or 0))
    except (apps.ApplicationError, ValueError) as exc:
        return redirect(f"/applications/{app_id}", err=str(exc))
    return redirect(f"/applications/{app_id}", msg="Resume version selected")


@router.post("/applications/{app_id:int}/approve-submission")
def approve_submission(app_id: int, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    try:
        apps.approve_submission(db, app)
    except apps.ApplicationError as exc:
        return redirect(f"/applications/{app_id}", err=str(exc))
    return redirect(f"/applications/{app_id}", msg="Submission approved. Submit on the employer's site, then record the confirmation here.")


@router.post("/applications/{app_id:int}/evidence")
async def evidence(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    settings = request.app.state.settings
    data, filename = None, None
    async with request.form() as form:  # closes uploaded temp files when done
        upload = form.get("file")
        if isinstance(upload, UploadFile) or hasattr(upload, "read"):
            data = await upload.read(settings.max_upload_bytes + 1)
            filename = upload.filename
            data = data or None
        kind, text = str(form.get("kind") or ""), str(form.get("text") or "")
    try:
        apps.add_evidence(db, settings, app, kind, text, filename, data)
    except (apps.ApplicationError, storage.StorageError) as exc:
        return redirect(f"/applications/{app_id}#evidence", err=str(exc))
    return redirect(f"/applications/{app_id}#evidence", msg="Evidence added")


@router.get("/applications/evidence/{ev_id:int}")
def evidence_file(ev_id: int, request: Request, db: Session = Depends(get_db)):
    ev = get_or_404(db, ApplicationEvidence, ev_id)
    if not ev.file_path:
        return redirect("/applications", err="File not found")
    path = storage.resolve(request.app.state.settings, ev.file_path)
    return FileResponse(path, filename=ev.original_filename or path.name, content_disposition_type="attachment")


@router.post("/applications/{app_id:int}/submitted")
async def submitted(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    try:
        apps.record_submission(db, app, form.get("method") or "", form.get("note") or "", form.get("attest") == "1")
    except apps.ApplicationError as exc:
        return redirect(f"/applications/{app_id}#submit", err=str(exc))
    return redirect(f"/applications/{app_id}", msg="Recorded as applied")


@router.post("/applications/{app_id:int}/notes")
async def notes(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    app.notes = (form.get("notes") or "")[:10000] or None
    return redirect(f"/applications/{app_id}", msg="Notes saved")


@router.post("/applications/{app_id:int}/assist")
def assist(app_id: int, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    app = get_or_404(db, Application, app_id)
    if not settings.browser_enabled:
        return redirect(f"/applications/{app_id}", err="Browser assistance is disabled. Set JOBPILOT_BROWSER_ENABLED=true in .env.")
    if app.status not in ("preparing", "ready_for_review"):
        return redirect(f"/applications/{app_id}", err="Browser assistance is available while preparing / ready for review")
    log_file = settings.log_dir / "browser-assist.log"
    with open(log_file, "ab") as out:  # noqa: PTH123
        subprocess.Popen([sys.executable, "-m", "jobpilot.cli", "assist", str(app.id)],  # no shell
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return redirect(f"/applications/{app_id}", msg="Opening a browser window. Complete and submit the form yourself, "
                                                   "then record the confirmation here. Refresh to see the run status.")


# ---- Review queue ---------------------------------------------------------------

@router.get("/review")
def review_page(request: Request, db: Session = Depends(get_db)):
    q = review.queue(db)
    return render(request, "review.html", q=q)


@router.post("/review/{item_id:int}")
async def review_resolve(item_id: int, request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    review.resolve(db, item_id, form.get("status") or "resolved", form.get("note"))
    return redirect("/review", msg="Updated")


@router.post("/review/job/{job_id:int}")
async def review_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    action = form.get("action")
    if action == "dismiss":
        from jobpilot.services.jobs import set_user_status

        set_user_status(db, job, "dismissed")
    elif action == "shortlist":
        from jobpilot.services.jobs import set_user_status

        set_user_status(db, job, "shortlisted")
    review.mark_job_reviewed(db, job, action or "reviewed")
    return redirect(form.get("next") or "/review", msg="Reviewed")
