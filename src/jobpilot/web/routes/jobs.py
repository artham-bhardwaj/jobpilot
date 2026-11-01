"""Job explorer, job detail, manual/URL/file job entry and source configuration."""

from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, UploadFile
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Job, JobSource, SourceRun
from jobpilot.services import jobs as job_svc
from jobpilot.services import job_export, matching, resumes as rs, settings_store
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


LEVELS = (("fresher", "Fresher / SDE-1", "Entry-level titles, 0–2 years asked, or level not stated"),
          ("fresher_stated", "Confirmed only", "Hide jobs that don't state a level"),
          ("any", "All levels", "Include SDE II, senior and other levels"))
REGION_TABS = (("", "All my regions"), ("india", "India"), ("remote", "Remote"), ("dubai", "Dubai / UAE"))


def _explorer_filters(qp) -> tuple[dict, str, str, str, bool]:
    """The Jobs page's search filters from its query string (shared by the page and its exports)."""
    hidden_view = qp.get("hidden") == "cleanup"
    # The cleanup view lists exactly what was hidden, so the focus filters (which hid them) are off there.
    level = "any" if hidden_view else (qp.get("level") or "fresher")
    roles = "any" if hidden_view else (qp.get("roles") or "mine")
    region = "any" if hidden_view else (qp.get("region") or "")
    filters = {
        "q": qp.get("q") or None,
        "status": qp.get("status", "" if hidden_view else "active") or None,
        "category": qp.get("category") or None,
        "eligibility": qp.get("eligibility") or None,
        "remote": qp.get("remote") or None,
        "user_status": qp.get("user_status") or None,
        "min_score": _int(qp.get("min_score")),
        "source_id": _int(qp.get("source_id")),
        "posted_within_days": _int(qp.get("posted_within_days")),
        "region": region or "mine",
        "applied": qp.get("applied") if qp.get("applied") in ("yes", "no") else None,
        "level": level if level in ("fresher", "fresher_stated") else None,
        "roles_only": roles != "any",
        "hidden": "cleanup" if hidden_view else None,
        "sort": qp.get("sort", "relevance"),
        "sector": "fintech" if qp.get("sector") == "fintech" else None,
    }
    return filters, level, roles, region, hidden_view


def export_response(fmt: str, jobs: list, title: str, parts: list[str], stem: str):
    """A PDF or Excel download of ``jobs`` (title, company, location, apply link) to share."""
    if fmt not in ("pdf", "xlsx"):
        raise HTTPException(404)
    rows = job_export.rows_for(jobs)
    subtitle = " · ".join([*parts, f"{len(rows)} job{'' if len(rows) == 1 else 's'}",
                           f"exported {date.today():%d %b %Y}"])
    if fmt == "pdf":
        body, media = job_export.to_pdf(rows, title=f"JobPilot: {title}", subtitle=subtitle), "application/pdf"
    else:
        body = job_export.to_xlsx(rows, title=title)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{job_export.filename(stem, fmt)}"'})


@router.get("/jobs/export.{fmt}")
def explorer_export(fmt: str, request: Request, db: Session = Depends(get_db)):
    """Every job the Jobs page shows with these filters (all pages, not just the current one)."""
    filters, level, _, region, hidden_view = _explorer_filters(request.query_params)
    jobs, _ = job_svc.search(db, limit=job_export.MAX_ROWS, **filters)
    parts = ["Hidden by cleanup"] if hidden_view else [
        "All levels" if level == "any" else "Fresher & SDE-1",
        {"india": "India", "remote": "Remote", "dubai": "Dubai / UAE", "other": "Other places",
         "any": "Any region"}.get(region, "India, Dubai and remote")]
    if filters["q"]:
        parts.append(f'search "{filters["q"]}"')
    if filters["sector"] == "fintech":
        return export_response(fmt, jobs, "Fintech jobs", parts, "jobpilot-fintech-jobs")
    return export_response(fmt, jobs, "Jobs", parts, "jobpilot-jobs")


@router.get("/jobs")
def explorer(request: Request, db: Session = Depends(get_db)):
    """Defaults to your focus: fresher / SDE-1 roles, in India, remote or Dubai, in your role categories.
    ``level=any``, ``region=any`` and ``roles=any`` widen it."""
    return _explorer_page(request, db, base="/jobs")


@router.get("/fintech")
def fintech_jobs(request: Request, db: Session = Depends(get_db)):
    """The Jobs page limited to fintech, bank and exchange companies (services/sectors.py), with the same filters."""
    return _explorer_page(request, db, base="/fintech", sector="fintech")


def _explorer_page(request: Request, db: Session, *, base: str, sector: str | None = None):
    from jobpilot.services import cleanup, search_query

    qp = request.query_params
    filters, level, roles, region, hidden_view = _explorer_filters(qp)
    if sector:
        filters["sector"] = sector
    page = max(1, _int(qp.get("page"), 1))
    jobs, total = job_svc.search(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **filters)
    sources = list(db.scalars(select(JobSource).order_by(JobSource.name)))
    from urllib.parse import urlencode

    from jobpilot.models import Application

    ids = [j.id for j in jobs]
    app_by_job = {a.job_id: a for a in db.scalars(select(Application).where(Application.job_id.in_(ids)))} if ids else {}

    def qs(*drop: str) -> str:
        return urlencode([(k, v) for k, v in qp.items() if k not in ("page", *drop)])

    # Each region tab counts with every other filter applied, so the numbers match what the tab shows.
    region_counts = {key: job_svc.search(db, limit=0, **{**filters, "region": key or "mine"})[1]
                     for key, _ in REGION_TABS}
    query = search_query.parse(filters["q"])
    widen = None
    if not total and query.terms and not hidden_view:  # nothing found: would a wider search find something?
        wide = job_svc.search(db, limit=0, **{**filters, "level": None, "roles_only": False, "region": "any"})[1]
        if wide:
            widen = {"count": wide, "qs": urlencode([(k, v) for k, v in qp.items() if k not in ("page", "level", "roles", "region")]
                                                    + [("level", "any"), ("roles", "any"), ("region", "any")])}
    hidden = {}
    if level != "any":
        hidden["level"] = job_svc.search(db, limit=0, **{**filters, "level": None})[1] - total
    if roles != "any":
        hidden["roles"] = job_svc.search(db, limit=0, **{**filters, "roles_only": False})[1] - total
    return render(request, "jobs.html", jobs=jobs, total=total, page=page, pages=(total + PAGE_SIZE - 1) // PAGE_SIZE,
                  filters=filters, level=level, roles=roles, region=region, levels=LEVELS, region_tabs=REGION_TABS,
                  hidden=hidden, query=query, hl=lambda t: search_query.highlight(t, query), widen=widen, hidden_view=hidden_view, cleanup_label=cleanup.label,
                  cleaned=cleanup.hidden_count(db), cleanup_preview=len(cleanup.plan(db)["hide"]),
                  skill_chips=_skill_chips(db, filters), categories=settings_store.get(db, "role_categories"), sources=sources,
                  base_qs=qs(), noq_qs=qs("q"), tab_qs=qs("region"), level_qs=qs("level"), roles_qs=qs("roles"),
                  app_by_job=app_by_job, region_counts=region_counts, base=base, sector=filters["sector"],
                  export_qs=urlencode([(k, v) for k, v in qp.items() if k not in ("page", "sector")]
                                      + ([("sector", filters["sector"])] if filters["sector"] else [])),
                  threshold=settings_store.get(db, "matching")["relevance_threshold"])


def _skill_chips(db: Session, filters: dict, n: int = 10) -> list[str]:
    """Your confirmed skills that the jobs in the current focus ask for most (quick search chips)."""
    from collections import Counter

    from jobpilot.models import Skill

    mine = {s.normalized_name for s in db.scalars(select(Skill).where(Skill.status == "confirmed"))}
    base = {**filters, "q": None}
    jobs, _ = job_svc.search(db, limit=1000, **base)
    counts = Counter(k for j in jobs for k in set(((j.match_details or {}).get("skills") or {}).get("mandatory") or [])
                     | set(((j.match_details or {}).get("skills") or {}).get("preferred") or []) if k in mine)
    return [k for k, _ in counts.most_common(n)]


@router.post("/jobs/cleanup")
def cleanup_run(db: Session = Depends(get_db)):
    from jobpilot.services import cleanup

    res = cleanup.run(db)
    db.commit()
    parts = ", ".join(f"{v} {cleanup.REASONS[k]}" for k, v in res["by_reason"].items() if v)
    return redirect("/jobs", msg=f"Hid {res['hidden']} irrelevant job(s){': ' + parts if parts else ''}. "
                                 "Nothing was deleted; see 'Hidden by cleanup' to restore.")


@router.post("/jobs/cleanup/undo")
def cleanup_undo(db: Session = Depends(get_db)):
    from jobpilot.services import cleanup

    n = cleanup.undo(db)
    db.commit()
    return redirect("/jobs", msg=f"Restored {n} job(s) hidden by the cleanup")


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
    from jobpilot.services import cleanup

    n = matching.rematch_all(db)
    hidden = cleanup.run(db, trigger="rematch")["hidden"] if settings_store.get(db, "job_cleanup")["auto"] else 0
    db.commit()
    return redirect("/jobs", msg=f"Re-scored {n} active job(s) with current profile and settings"
                    + (f"; hid {hidden} that no longer fit (see Hidden by cleanup)" if hidden else ""))


@router.get("/jobs/{job_id:int}")
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)  # read-only: unscored jobs show a "Re-score" prompt instead
    selection = rs.select_for_job(db, job)
    extra = _job_extras(db, job)
    from jobpilot.services import tailored

    return render(request, "job_detail.html", job=job, m=job.match_details or {}, selection=selection,
                  categories=settings_store.get(db, "role_categories"), tailored_resume=tailored.latest(db, job.id),
                  tailored_eligible=tailored.eligible(db, job), tailored_compiler=tailored.compiler() is not None,
                  tailored_template=tailored.template_info(db) is not None,
                  approved_resumes=_approved_resumes(db), today=date.today().isoformat(), **extra)


def _approved_resumes(db: Session) -> list:
    from jobpilot.models import ResumeVersion

    return list(db.scalars(select(ResumeVersion).where(ResumeVersion.status == "approved")
                           .order_by(ResumeVersion.id.desc())))


@router.post("/jobs/{job_id:int}/tailored-resume")
async def tailored_generate(job_id: int, request: Request, db: Session = Depends(get_db)):
    """Generate (or reuse) this job's tailored resume, then validate it. Never submits anything."""
    from jobpilot.services import tailored

    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    back = _local_next(form.get("next"), f"/jobs/{job_id}#tailored")
    try:
        tr = tailored.generate(db, request.app.state.settings, job, force=bool(form.get("force")))
    except tailored.TailorError as exc:
        return redirect(back, err=str(exc))
    db.commit()
    if tr.status == "valid":
        return redirect(back, msg=f"Resume for {job.title} at {job.company_name or 'this company'} is ready and validated")
    return redirect(back, err=f"Tailored resume not generated: {(tr.error or '')[:300]}")


def _local_next(nxt: str | None, default: str) -> str:
    """A return path from a form, only if it is a local Jobs / Job desk path."""
    if nxt and not nxt.startswith("//") and nxt.startswith(("/jobs", "/desk")):
        return nxt
    return default


@router.get("/tailored-resumes/{tr_id:int}/download")
def tailored_download(tr_id: int, request: Request, db: Session = Depends(get_db)):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from jobpilot.models import TailoredResume
    from jobpilot.services import tailored

    tr = get_or_404(db, TailoredResume, tr_id)
    settings = request.app.state.settings
    if tr.status not in ("valid", "superseded", "outdated") or not tailored.file_ok(settings, tr):
        raise HTTPException(404, "No valid file for this tailored resume")
    return FileResponse(settings.files_dir / tr.pdf_path, media_type="application/pdf",
                        filename=f"{tailored._slug(tr.company_name)}_{tr.job_id}_resume.pdf")


@router.get("/jobs/{job_id:int}/resume-review")
def resume_review(job_id: int, request: Request, db: Session = Depends(get_db)):
    """Read-only: why a resume was (or wasn't) selected for this job, against your verified facts."""
    from jobpilot.models import Employment, Project
    from jobpilot.services import profile as profile_svc
    from jobpilot.services.skills_taxonomy import extract_skills

    job = get_or_404(db, Job, job_id)
    m = job.match_details or {}
    sk = m.get("skills") or {}
    job_skills = set(sk.get("mandatory") or []) | set(sk.get("preferred") or [])
    facts = []
    for e in db.scalars(select(Employment).where(Employment.status == "confirmed")):
        hits = sorted(job_skills & set(extract_skills(f"{e.title}\n{e.description or ''}")))
        facts.append({"kind": "work" + (" (internship)" if e.employment_type == "internship" else ""),
                      "label": f"{e.title} at {e.company}", "skills": hits})
    for pr in db.scalars(select(Project).where(Project.status == "confirmed")):
        hits = sorted(job_skills & set(extract_skills(f"{pr.name}\n{pr.description or ''}\n{', '.join(pr.technologies or [])}")))
        facts.append({"kind": "personal project", "label": pr.name, "skills": hits})
    return render(request, "resume_review.html", job=job, m=m, sk=sk, selection=rs.select_for_job(db, job),
                  facts=facts, skill_profile=profile_svc.skill_profile(db))


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
    result = job_svc.check_alive(db, PoliteHttp(request.app.state.settings), job, initiated_by_user=True)
    return redirect(f"/jobs/{job_id}", msg=f"Checked: {result}")


@router.post("/jobs/{job_id:int}/expire")
def job_expire(job_id: int, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    job_svc.mark_expired(db, job, reason="marked manually")
    return redirect(f"/jobs/{job_id}", msg="Marked expired")


# ---- Sources -----------------------------------------------------------------

@router.get("/sources")
def sources_page(request: Request, edit: int | None = None, db: Session = Depends(get_db)):
    editing = db.get(JobSource, edit) if edit else None
    from jobpilot.services import regional_sources

    return render(request, "sources.html", health=job_svc.source_health(db), adapters=ADAPTERS, editing=editing,
                  regional=regional_sources.plan(db))


@router.get("/sources/{source_id:int}")
def source_detail(source_id: int, request: Request, db: Session = Depends(get_db)):
    src = get_or_404(db, JobSource, source_id)
    runs = list(db.scalars(select(SourceRun).where(SourceRun.source_id == src.id).order_by(SourceRun.id.desc()).limit(50)))
    row = next(r for r in job_svc.source_health(db) if r["source"].id == src.id)
    return render(request, "source_detail.html", src=src, runs=runs, row=row, adapter=ADAPTERS.get(src.kind))


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


@router.post("/sources/regional")
def sources_regional(db: Session = Depends(get_db)):
    from jobpilot.services import regional_sources

    done = regional_sources.apply(db)
    if not done["add"] and not done["extend"]:
        return redirect("/sources", msg="The regional sources are already set up")
    return redirect("/sources", msg=f"Added {len(done['add'])} source(s) and extended {len(done['extend'])} with "
                                    "India / remote / Dubai. Run them (or dry-run them) below.")


@router.post("/sources/{source_id:int}/delete")
def source_delete(source_id: int, db: Session = Depends(get_db)):
    src = get_or_404(db, JobSource, source_id)
    if src is not None:
        db.delete(src)
    return redirect("/sources", msg="Source removed (its jobs are kept)")


@router.post("/sources/run")
async def sources_run(request: Request, background: BackgroundTasks):
    form = await form_dict(request)
    ids = [int(form["source_id"])] if (form.get("source_id") or "").isdigit() else None
    dry_run = form.get("dry_run") == "1"
    settings, factory = request.app.state.settings, request.app.state.sessionmaker

    def _job():
        run_task(settings, factory, "collect_jobs_dry_run" if dry_run else "collect_jobs",
                 lambda s: job_svc.collect_all(s, settings, source_ids=ids, dry_run=dry_run), trigger="manual")

    background.add_task(_job)
    what = "Dry run" if dry_run else "Collection"
    back = f"/sources/{ids[0]}" if ids else "/sources"
    return redirect(back, msg=f"{what} started in the background — refresh to see the result"
                              + (" (nothing will be saved)" if dry_run else ""))
