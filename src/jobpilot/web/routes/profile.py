"""Candidate profile and verified facts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from jobpilot.services import profile as ps
from jobpilot.web.deps import first_error, form_dict, get_db, get_or_404, redirect, render

router = APIRouter(prefix="/profile")

FACT_STATUS = [("unverified", "unverified"), ("confirmed", "confirmed"), ("outdated", "outdated / rejected")]
SKILL_STATUS = [("unverified", "unverified"), ("learning", "learning"), ("confirmed", "confirmed"),
                ("outdated", "outdated / rejected")]
ROLE_TYPES = [("", "not recorded"), ("software_development", "software development"), ("testing_qa", "testing / QA"),
              ("data", "data / analytics"), ("devops_infra", "DevOps / infrastructure"), ("support", "support / operations"),
              ("other", "other")]
PROJECT_KINDS = [("personal", "personal project"), ("academic", "academic / coursework"),
                 ("learning_exercise", "learning exercise / tutorial"), ("open_source", "open-source contribution")]

# Field specs used to render add/edit forms: (name, label, type, options)
FACT_FIELDS = {
    "employment": [("company", "Company *", "text", None), ("title", "Title *", "text", None),
                   ("location", "Location", "text", None),
                   ("employment_type", "Type", "select", ["", "full_time", "part_time", "internship", "contract", "freelance"]),
                   ("role_type", "Kind of work (only software development counts as development experience)", "select", ROLE_TYPES),
                   ("start_date", "Start date", "date", None), ("end_date", "End date (blank = current)", "date", None),
                   ("description", "Description / achievements (one per line)", "textarea", None),
                   ("status", "Status", "select", FACT_STATUS), ("source", "Source", "text", None)],
    "education": [("institution", "Institution *", "text", None), ("degree", "Degree", "text", None),
                  ("field_of_study", "Field of study", "text", None), ("start_date", "Start", "date", None),
                  ("end_date", "End", "date", None), ("grade", "Grade", "text", None),
                  ("notes", "Notes", "textarea", None), ("status", "Status", "select", FACT_STATUS),
                  ("source", "Source", "text", None)],
    "projects": [("name", "Name *", "text", None), ("kind", "Kind (never counted as work experience)", "select", PROJECT_KINDS),
                 ("url", "URL", "url", None),
                 ("technologies", "Technologies (comma separated)", "text", None),
                 ("start_date", "Start", "date", None), ("end_date", "End", "date", None),
                 ("description", "Description (one point per line)", "textarea", None),
                 ("status", "Status", "select", FACT_STATUS), ("source", "Source", "text", None)],
    "skills": [("name", "Skill *", "text", None), ("category", "Category", "text", None),
               ("years", "Years used", "number", None), ("evidence", "Evidence (where you used it)", "textarea", None),
               ("status", "Status", "select", SKILL_STATUS), ("source", "Source", "text", None)],
    "certifications": [("name", "Certification *", "text", None), ("issuer", "Issuer", "text", None),
                       ("issued_on", "Issued", "date", None), ("expires_on", "Expires", "date", None),
                       ("credential_url", "Credential URL", "url", None),
                       ("status", "Status", "select", FACT_STATUS), ("source", "Source", "text", None)],
    "achievements": [("title", "Achievement *", "text", None),
                     ("context", "Where (keeps work and non-work achievements apart)", "select",
                      [("work", "at work"), ("academic", "academic"), ("project", "personal project"), ("other", "other")]),
                     ("achieved_on", "Date", "date", None),
                     ("description", "Details (only what you can back up — no estimated metrics)", "textarea", None),
                     ("status", "Status", "select", FACT_STATUS), ("source", "Source", "text", None)],
    "links": [("label", "Label *", "text", None), ("url", "URL *", "url", None)],
}
KIND_LABELS = {"employment": "Employment", "education": "Education", "projects": "Projects", "skills": "Skills",
               "certifications": "Certifications", "achievements": "Achievements", "links": "Links"}
SECTION_ORDER = ["skills", "employment", "education", "projects", "certifications", "achievements", "links"]


def _check_kind(kind: str) -> None:
    if kind not in ps.FACT_MODELS:
        raise HTTPException(status_code=404,
                            detail=f"Unknown profile section '{kind[:40]}'. Valid sections: {', '.join(ps.FACT_MODELS)}")


@router.get("")
def profile_page(request: Request, db: Session = Depends(get_db)):
    facts = {k: ps.list_facts(db, k) for k in SECTION_ORDER}
    years, years_src = ps.experience_years(db)
    return render(request, "profile.html", profile=ps.get_profile(db), facts=facts, fields=FACT_FIELDS,
                  labels=KIND_LABELS, sections=SECTION_ORDER, completeness=ps.completeness(db), years=years,
                  years_src=years_src, experience=ps.experience_breakdown(db))


@router.post("")
async def profile_save(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        ps.update_profile(db, ps.ProfileIn.model_validate(form))
    except ValidationError as exc:
        return redirect("/profile", err=first_error(exc))
    return redirect("/profile", msg="Profile saved. Review it and click Confirm when accurate.")


@router.post("/confirm")
def profile_confirm(db: Session = Depends(get_db)):
    ps.confirm_profile(db)
    return redirect("/profile", msg="Profile details confirmed")


@router.get("/{kind}/{fact_id:int}/edit")
def fact_edit_page(kind: str, fact_id: int, request: Request, db: Session = Depends(get_db)):
    _check_kind(kind)
    model, _ = ps.FACT_MODELS[kind]
    obj = get_or_404(db, model, fact_id)
    return render(request, "fact_edit.html", kind=kind, obj=obj, fields=FACT_FIELDS[kind], label=KIND_LABELS[kind])


@router.post("/{kind}")
@router.post("/{kind}/{fact_id:int}")
async def fact_save(kind: str, request: Request, fact_id: int | None = None, db: Session = Depends(get_db)):
    _check_kind(kind)
    form = await form_dict(request)
    _, schema = ps.FACT_MODELS[kind]
    try:
        ps.save_fact(db, kind, schema.model_validate(form), fact_id)
    except (ValidationError, ValueError, LookupError) as exc:
        return redirect(f"/profile#{kind}", err=first_error(exc))
    return redirect(f"/profile#{kind}", msg=f"{KIND_LABELS[kind]} saved")


@router.post("/{kind}/{fact_id:int}/status")
async def fact_status(kind: str, fact_id: int, request: Request, db: Session = Depends(get_db)):
    _check_kind(kind)
    form = await form_dict(request)
    try:
        ps.set_fact_status(db, kind, fact_id, form.get("status") or "")
    except (ValueError, LookupError) as exc:
        return redirect(f"/profile#{kind}", err=str(exc))
    return redirect(f"/profile#{kind}", msg="Status updated")


@router.post("/{kind}/{fact_id:int}/delete")
def fact_delete(kind: str, fact_id: int, db: Session = Depends(get_db)):
    _check_kind(kind)
    ps.delete_fact(db, kind, fact_id)
    return redirect(f"/profile#{kind}", msg="Deleted")
