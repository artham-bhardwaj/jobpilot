"""Job desk: every job with its own resume, apply and delete actions on one page."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Application, Job, TailoredResume
from jobpilot.services import jobs as job_svc
from jobpilot.services import resumes as rs
from jobpilot.services import search_query, settings_store, tailored
from jobpilot.web.deps import form_dict, get_db, get_or_404, redirect, render

router = APIRouter()
PAGE_SIZE = 20
# (key, label, search filters): which jobs a tab shows.
TABS = (
    ("todo", "To apply", {"applied": "no", "user_status": None}),
    ("saved", "Saved", {"applied": "no", "user_status": "shortlisted"}),
    ("applied", "Applied", {"applied": "yes", "user_status": None}),
    ("all", "All", {"applied": None, "user_status": None}),
)


def _int(v, default=None):
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _desk_filters(qp) -> tuple[str, bool, str | None, dict, dict]:
    """The desk's tab, scope, search and filters from its query string (shared by the page and its exports)."""
    tab = qp.get("tab") if qp.get("tab") in {t[0] for t in TABS} else "todo"
    wide = qp.get("scope") == "any"
    q = qp.get("q") or None
    base = {"q": q, "status": "active" if tab != "applied" else None, "sort": qp.get("sort") or "relevance",
            "region": "any" if wide else "mine", "level": None if wide else "fresher", "roles_only": not wide}
    return tab, wide, q, base, {**base, **next(f for k, _, f in TABS if k == tab)}


@router.get("/desk/export.{fmt}")
def desk_export(fmt: str, request: Request, db: Session = Depends(get_db)):
    """Every job in the current desk tab (all pages), as PDF or Excel."""
    from jobpilot.services import job_export
    from jobpilot.web.routes.jobs import export_response

    tab, wide, q, _, filters = _desk_filters(request.query_params)
    jobs, _ = job_svc.search(db, limit=job_export.MAX_ROWS, **filters)
    parts = [dict((k, label) for k, label, _ in TABS)[tab], "All jobs" if wide else "My focus"]
    if q:
        parts.append(f'search "{q}"')
    return export_response(fmt, jobs, "Job desk", parts, "jobpilot-desk")


@router.get("/desk")
def desk(request: Request, db: Session = Depends(get_db)):
    """Your focus by default (fresher / SDE-1, India / remote / Dubai, your roles); ``scope=any`` widens it."""
    qp = request.query_params
    tab, wide, q, base, filters = _desk_filters(qp)
    page = max(1, _int(qp.get("page"), 1))
    jobs, total = job_svc.search(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **filters)
    counts = {k: job_svc.search(db, limit=0, **{**base, "status": "active" if k != "applied" else None, **f})[1]
              for k, _, f in TABS}

    ids = [j.id for j in jobs]
    apps = {a.job_id: a for a in db.scalars(select(Application).where(Application.job_id.in_(ids)))} if ids else {}
    settings = request.app.state.settings
    trs: dict[int, TailoredResume] = {}
    for tr in db.scalars(select(TailoredResume).where(TailoredResume.job_id.in_(ids)).order_by(TailoredResume.id)):
        trs[tr.job_id] = tr  # latest wins
    cards = []
    for j in jobs:
        tr = trs.get(j.id)
        cards.append({"job": j, "app": apps.get(j.id), "tr": tr,
                      "tr_ready": bool(tr and tr.status == "valid" and tailored.file_ok(settings, tr)),
                      "selection": rs.select_for_job(db, j)})

    def qs(**over) -> str:
        cur = {k: v for k, v in qp.items() if k not in ("page", "msg", "err")}
        cur.update({k: v for k, v in over.items()})
        return urlencode({k: v for k, v in cur.items() if v not in (None, "")})

    query = search_query.parse(q)
    from jobpilot.models import ResumeVersion

    approved = list(db.scalars(select(ResumeVersion).where(ResumeVersion.status == "approved")
                               .order_by(ResumeVersion.id.desc())))
    return render(request, "desk.html", cards=cards, total=total, page=page, pages=(total + PAGE_SIZE - 1) // PAGE_SIZE,
                  tab=tab, tabs=TABS, counts=counts, wide=wide, q=q or "", query=query,
                  hl=lambda t: search_query.highlight(t, query), qs=qs, sort=base["sort"],
                  here=f"/desk?{qs(page=page if page > 1 else None)}", approved_resumes=approved,
                  today=date.today().isoformat(), template_ok=tailored.template_info(db) is not None,
                  compiler_ok=tailored.compiler() is not None,
                  tailor_enabled=settings_store.get(db, "tailored_resumes").get("enabled", True),
                  categories=settings_store.get(db, "role_categories"))


@router.post("/desk/{job_id:int}/delete")
async def delete_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    from jobpilot.services import job_delete

    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    nxt = form.get("next") or "/desk"
    back = nxt if nxt.startswith(("/desk", "/jobs")) and not nxt.startswith("//") else "/desk"
    try:
        label = job_delete.delete(db, request.app.state.settings, job)
    except job_delete.DeleteError as exc:
        return redirect(back, err=str(exc))
    db.commit()
    return redirect(back, msg=f"Deleted: {label}. It won't be added again by your sources.")
