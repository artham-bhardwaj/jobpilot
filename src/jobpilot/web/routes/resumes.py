"""Resume library: slots, imports, versions, approval, generation, downloads."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Education, Employment, Project, Resume, ResumeVersion, Skill
from jobpilot.services import profile as ps
from jobpilot.services import resumes as rs
from jobpilot.services import settings_store, storage
from jobpilot.web.deps import form_dict, get_db, get_or_404, redirect, render

router = APIRouter(prefix="/resumes")


@router.get("")
def resumes_page(request: Request, db: Session = Depends(get_db)):
    cats = settings_store.get(db, "role_categories")
    options = [("master", "Master")] + [(k, v.get("label", k)) for k, v in cats.items()]
    return render(request, "resumes.html", resumes=rs.list_resumes(db), categories=cats, category_options=options)


@router.post("/slots")
def create_slots(db: Session = Depends(get_db)):
    n = rs.create_standard_slots(db)
    return redirect("/resumes", msg=f"Created {n} resume slot(s)")


@router.post("")
async def create(request: Request, db: Session = Depends(get_db)):
    f = await form_dict(request)
    try:
        r = rs.create_resume(db, f.get("name") or "", f.get("category") or "", description=f.get("description"))
    except rs.ResumeError as exc:
        return redirect("/resumes", err=str(exc))
    return redirect(f"/resumes/{r.id}", msg="Resume created")


@router.get("/{resume_id:int}")
def detail(resume_id: int, request: Request, db: Session = Depends(get_db)):
    r = get_or_404(db, Resume, resume_id)
    confirmed = {
        "employment": list(db.scalars(select(Employment).where(Employment.status == "confirmed"))),
        "projects": list(db.scalars(select(Project).where(Project.status == "confirmed"))),
        "education": list(db.scalars(select(Education).where(Education.status == "confirmed"))),
        "skills": list(db.scalars(select(Skill).where(Skill.status == "confirmed").order_by(Skill.name))),
    }
    return render(request, "resume_detail.html", resume=r, confirmed=confirmed, profile=ps.get_profile(db))


@router.post("/{resume_id:int}/upload")
async def upload(resume_id: int, request: Request, file: UploadFile, db: Session = Depends(get_db)):
    r = get_or_404(db, Resume, resume_id)
    settings = request.app.state.settings
    data = await file.read(settings.max_upload_bytes + 1)
    form = await form_dict(request)
    try:
        v = rs.import_version(db, settings, r, file.filename or "resume", data, change_summary=form.get("change_summary"))
    except (rs.ResumeError, storage.StorageError) as exc:
        return redirect(f"/resumes/{resume_id}", err=str(exc))
    if form.get("suggest_skills") and v.extracted_text:
        created = ps.suggest_skills_from_text(db, v.extracted_text, f"{r.name} v{v.version_no}")
        if created:
            return redirect(f"/resumes/{resume_id}", msg=f"Imported v{v.version_no} (draft). {len(created)} skill(s) "
                                                         "added to your profile as UNVERIFIED — review them on the Profile page.")
    return redirect(f"/resumes/{resume_id}", msg=f"Imported v{v.version_no} as draft — review and approve it")


@router.post("/{resume_id:int}/generate")
async def generate(resume_id: int, request: Request, db: Session = Depends(get_db)):
    r = get_or_404(db, Resume, resume_id)
    form = await request.form()
    selection = {k: [int(x) for x in form.getlist(k) if str(x).isdigit()]
                 for k in ("employment", "projects", "education", "skills")}
    try:
        v = rs.generate_version(db, request.app.state.settings, r, selection, change_summary=form.get("change_summary") or None)
    except rs.ResumeError as exc:
        return redirect(f"/resumes/{resume_id}", err=str(exc))
    return redirect(f"/resumes/{resume_id}", msg=f"Generated v{v.version_no} as draft — review the file before approving")


@router.post("/versions/{version_id:int}/{action}")
def version_action(version_id: int, action: str, db: Session = Depends(get_db)):
    v = get_or_404(db, ResumeVersion, version_id)
    try:
        if action == "approve":
            rs.approve_version(db, v)
        elif action == "archive":
            rs.archive_version(db, v)
        else:
            return redirect(f"/resumes/{v.resume_id}", err="Unknown action")
    except rs.ResumeError as exc:
        return redirect(f"/resumes/{v.resume_id}", err=str(exc))
    return redirect(f"/resumes/{v.resume_id}", msg=f"Version {v.version_no} {action}d")


@router.get("/versions/{version_id:int}/download")
def download(version_id: int, request: Request, db: Session = Depends(get_db)):
    v = get_or_404(db, ResumeVersion, version_id)
    if not v.file_path:
        return redirect("/resumes", err="File not found")
    path = storage.resolve(request.app.state.settings, v.file_path)
    return FileResponse(path, filename=v.original_filename or path.name, media_type=v.mime_type or "application/octet-stream",
                        content_disposition_type="attachment")


@router.post("/{resume_id:int}/toggle")
def toggle(resume_id: int, db: Session = Depends(get_db)):
    r = get_or_404(db, Resume, resume_id)
    r.active = not r.active
    return redirect("/resumes", msg="Updated")
