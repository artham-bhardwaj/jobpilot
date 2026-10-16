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
    from jobpilot.services import answers as answer_svc

    from jobpilot.automation.apply import application_host

    return render(request, "application_detail.html", app=app, job=app.job, checklist=checklist, host=application_host(app),
                  questions=answer_svc.questions(db, app), prep=app.preparation or {},
                  next_states=sorted(apps.TRANSITIONS.get(app.status, set()) - {"applied"}),
                  approved_versions=approved, duplicates=apps.find_duplicates(db, app.job, exclude_id=app.id),
                  manual=manual_fields(db), site=site_for(app.apply_url),
                  browser_enabled=settings.browser_enabled, confirmation_kinds=sorted(apps.CONFIRMATION_KINDS),
                  evidence_kinds=sorted(apps.EVIDENCE_KINDS), next_step=apps.next_action(db, app))


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
async def assist(app_id: int, request: Request, db: Session = Depends(get_db)):
    from jobpilot.automation.apply import application_host

    settings = request.app.state.settings
    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    if not settings.browser_enabled:
        return redirect(f"/applications/{app_id}", err="Browser assistance is disabled. Set JOBPILOT_BROWSER_ENABLED=true in .env.")
    if app.status not in ("preparing", "ready_for_review"):
        return redirect(f"/applications/{app_id}", err="Browser assistance is available while preparing / ready for review")
    host = application_host(app)
    if not host:
        return redirect(f"/applications/{app_id}", err="No application URL to open")
    if form.get("approve") != "1" or (form.get("approve_host") or "").lower() != host.lower():
        return redirect(f"/applications/{app_id}", err=f"Tick the box to approve opening {host} for this application")
    log_file = settings.log_dir / "browser-assist.log"
    with open(log_file, "ab") as out:  # noqa: PTH123
        subprocess.Popen([sys.executable, "-m", "jobpilot.cli", "assist", str(app.id), "--approve-host", host],  # no shell
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return redirect(f"/applications/{app_id}", msg=f"Opening {host} in a browser window. Complete and submit the form "
                                                   "yourself, then record the confirmation here. Refresh to see the run status.")


@router.post("/applications/{app_id:int}/prepare")
def prepare(app_id: int, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    apps.prepare_snapshot(db, app)
    return redirect(f"/applications/{app_id}#preparation", msg="Preparation record refreshed")


@router.post("/applications/{app_id:int}/questions")
async def add_questions(app_id: int, request: Request, db: Session = Depends(get_db)):
    from jobpilot.services import answers as answer_svc

    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    added = answer_svc.add_questions(db, app, (form.get("questions") or "").splitlines())
    return redirect(f"/applications/{app_id}#questions", msg=f"{len(added)} question(s) added")


@router.post("/applications/questions/{q_id:int}")
async def answer_question(q_id: int, request: Request, db: Session = Depends(get_db)):
    from jobpilot.models import AnswerBankEntry, ApplicationQuestion
    from jobpilot.services import answers as answer_svc

    q = get_or_404(db, ApplicationQuestion, q_id, "question")
    form = await form_dict(request)
    back = f"/applications/{q.application_id}#questions"
    try:
        action = form.get("action")
        if action == "use_bank":
            entry = get_or_404(db, AnswerBankEntry, int(form.get("entry_id") or 0), "answer")
            answer_svc.set_answer(db, q, "", from_bank=entry)
            msg = "Suggested answer copied — review and approve it"
        elif action == "approve":
            answer_svc.approve(db, q, answered_myself=form.get("answered_myself") == "1")
            msg = "Answer approved"
        elif action == "delete":
            db.delete(q)
            msg = "Question removed"
        else:
            answer_svc.set_answer(db, q, form.get("answer") or "")
            msg = "Answer saved — approve it when final"
    except (ValueError, LookupError) as exc:
        return redirect(back, err=str(exc))
    return redirect(back, msg=msg)


@router.get("/answers")
def answer_bank(request: Request, db: Session = Depends(get_db)):
    from jobpilot.models import AnswerBankEntry
    from jobpilot.models.applications import ANSWER_CATEGORIES
    from jobpilot.services import answers as answer_svc
    from jobpilot.services import settings_store

    entries = list(db.scalars(select(AnswerBankEntry).order_by(AnswerBankEntry.category, AnswerBankEntry.id)))
    have = [e.question for e in entries]
    unanswered = [(q, c) for q, c in answer_svc.COMMON_QUESTIONS if not any(answer_svc.similarity(q, h) >= 0.6 for h in have)]
    edit = db.get(AnswerBankEntry, int(request.query_params["edit"])) if request.query_params.get("edit", "").isdigit() else None
    return render(request, "answers.html", entries=entries, unanswered=unanswered, edit=edit,
                  categories=[c for c in ANSWER_CATEGORIES if c != "sensitive"],
                  role_categories=settings_store.get(db, "role_categories"))


@router.post("/answers")
async def answer_bank_save(request: Request, db: Session = Depends(get_db)):
    from jobpilot.services import answers as answer_svc

    form = await request.form()
    try:
        entry_id = int(form.get("entry_id")) if str(form.get("entry_id") or "").isdigit() else None
        e = answer_svc.save_entry(db, str(form.get("question") or ""), str(form.get("answer") or ""),
                                  str(form.get("category") or "general"), [str(x) for x in form.getlist("role_categories")],
                                  approved=form.get("approved") == "1", entry_id=entry_id)
    except (ValueError, LookupError) as exc:
        return redirect("/answers", err=str(exc))
    note = " Marked SENSITIVE: it will never be suggested or filled automatically." if e.sensitive else ""
    return redirect("/answers", msg="Answer saved." + note)


@router.post("/answers/{entry_id:int}/delete")
def answer_bank_delete(entry_id: int, db: Session = Depends(get_db)):
    from jobpilot.models import AnswerBankEntry

    db.delete(get_or_404(db, AnswerBankEntry, entry_id, "answer"))
    return redirect("/answers", msg="Answer deleted")


# ---- Review queue ---------------------------------------------------------------

@router.get("/review")
def review_page(request: Request, db: Session = Depends(get_db)):
    from jobpilot.services import queue

    qp = request.query_params
    category = qp.get("category") if qp.get("category") in queue.CATEGORIES else None
    sort = qp.get("sort") if qp.get("sort") in ("priority", "newest", "score") else "priority"
    try:
        page = max(1, int(qp.get("page") or 1))
    except ValueError:
        page = 1
    result = queue.query(db, category=category, q=qp.get("q") or None, sort=sort, page=page)
    return render(request, "review.html", r=result, categories=queue.CATEGORIES, category=category or "",
                  sort=sort, q=qp.get("q") or "", bulk_labels={"mark_reviewed": "Mark reviewed", "shortlist": "Shortlist",
                                                                "dismiss_job": "Dismiss jobs", "acknowledge": "Acknowledge"})


@router.post("/review/bulk")
async def review_bulk(request: Request, db: Session = Depends(get_db)):
    from jobpilot.services import queue

    form = await request.form()
    keys = [str(k) for k in form.getlist("keys")][:200]
    action = str(form.get("action") or "")
    back = str(form.get("next") or "/review")
    if not back.startswith("/review"):
        back = "/review"
    if action not in queue.BULK_ACTIONS:
        return redirect(back, err="Choose an allowed bulk action")
    if not keys:
        return redirect(back, err="Select at least one item")
    res = queue.bulk_apply(db, keys, action)
    msg = f"{len(res['applied'])} item(s) updated"
    if res["refused"]:
        msg += f"; {len(res['refused'])} skipped ({res['refused'][0][1]})"
    return redirect(back, msg=msg)


@router.post("/review/ack")
async def review_ack(request: Request, db: Session = Depends(get_db)):
    from jobpilot.services import queue

    form = await form_dict(request)
    item = next((i for i in queue.build(db) if i["key"] == form.get("key")), None)
    if item is None:
        return redirect("/review", err="That item is no longer in the queue")
    try:
        queue.apply_action(db, item, "acknowledge")
    except ValueError as exc:
        return redirect("/review", err=str(exc))
    return redirect("/review", msg="Acknowledged")


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
