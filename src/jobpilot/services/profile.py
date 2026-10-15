"""Candidate profile and verified-facts repository.

Rules:
- Nothing is required at setup; every field is optional.
- Facts are only *used* (eligibility, resumes, drafts, autofill) when confirmed.
- Every change is audited with the names of changed fields (values are not
  duplicated into logs).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import CandidateProfile, Education, Employment, ProfileLink, Project, Skill, utcnow
from jobpilot.services import audit
from jobpilot.services.skills_taxonomy import canonical, category_of

EMAIL_PATTERN = r"^[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}$"
FactStatus = Literal["confirmed", "unverified"]
SkillStatus = Literal["confirmed", "learning", "unverified"]


def _split_list(v: Any) -> Any:
    if isinstance(v, str):
        return [p.strip() for p in v.replace("\n", ",").split(",") if p.strip()]
    return v


class _In(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


class ProfileIn(_In):
    full_name: str | None = Field(default=None, max_length=200)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    headline: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    phone: str | None = Field(default=None, max_length=50, pattern=r"^[0-9+()\-. ]{5,50}$")
    current_location: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=5000)
    total_experience_years: float | None = Field(default=None, ge=0, le=60)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    salary_expectation_min: int | None = Field(default=None, ge=0)
    salary_expectation_max: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=10)
    work_authorization: str | None = Field(default=None, max_length=300)
    needs_sponsorship: bool | None = None
    willing_to_relocate: bool | None = None
    acceptable_locations: list[str] | None = None
    remote_preferences: list[Literal["remote", "hybrid", "onsite"]] | None = None

    _lists = field_validator("acceptable_locations", "remote_preferences", mode="before")(_split_list)

    @field_validator("needs_sponsorship", "willing_to_relocate", mode="before")
    @classmethod
    def _tri(cls, v: Any) -> Any:
        if v in ("", None, "unknown"):
            return None
        if isinstance(v, str):
            return v.lower() in ("true", "yes", "1", "on")
        return v


class LinkIn(_In):
    label: str = Field(min_length=1, max_length=100)
    url: HttpUrl


class EducationIn(_In):
    institution: str = Field(min_length=1, max_length=200)
    degree: str | None = Field(default=None, max_length=200)
    field_of_study: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    end_date: date | None = None
    grade: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    status: FactStatus = "unverified"
    source: str | None = Field(default="manual entry", max_length=200)


class EmploymentIn(_In):
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    employment_type: Literal["full_time", "part_time", "internship", "contract", "freelance"] | None = None
    start_date: date | None = None
    end_date: date | None = None
    description: str | None = Field(default=None, max_length=5000)
    status: FactStatus = "unverified"
    source: str | None = Field(default="manual entry", max_length=200)


class ProjectIn(_In):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    url: HttpUrl | None = None
    technologies: list[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: FactStatus = "unverified"
    source: str | None = Field(default="manual entry", max_length=200)

    _lists = field_validator("technologies", mode="before")(_split_list)


class SkillIn(_In):
    name: str = Field(min_length=1, max_length=100)
    category: str | None = Field(default=None, max_length=50)
    status: SkillStatus = "unverified"
    years: float | None = Field(default=None, ge=0, le=60)
    evidence: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default="manual entry", max_length=200)


FACT_MODELS: dict[str, tuple[type, type[_In]]] = {
    "links": (ProfileLink, LinkIn),
    "education": (Education, EducationIn),
    "employment": (Employment, EmploymentIn),
    "projects": (Project, ProjectIn),
    "skills": (Skill, SkillIn),
}


def get_profile(session: Session) -> CandidateProfile:
    """The candidate profile. Read-only: if none exists yet, an empty *unsaved* profile is returned."""
    profile = session.scalar(select(CandidateProfile).limit(1))
    return profile if profile is not None else CandidateProfile()


def _profile_for_write(session: Session) -> CandidateProfile:
    profile = get_profile(session)
    if profile.id is None:
        session.add(profile)
        session.flush()
    return profile


def update_profile(session: Session, data: ProfileIn) -> CandidateProfile:
    profile = _profile_for_write(session)
    changed = []
    for key, value in data.model_dump().items():
        if getattr(profile, key) != value:
            setattr(profile, key, value)
            changed.append(key)
    if changed:
        profile.confirmed_at = None  # edited details need re-confirmation
        audit.record(session, "profile.updated", "candidate_profile", profile.id, fields=changed)
    return profile


def confirm_profile(session: Session) -> CandidateProfile:
    profile = _profile_for_write(session)
    profile.confirmed_at = utcnow()
    audit.record(session, "profile.confirmed", "candidate_profile", profile.id)
    return profile


def list_facts(session: Session, kind: str) -> list:
    model, _ = FACT_MODELS[kind]
    order = model.normalized_name if model is Skill else model.id
    return list(session.scalars(select(model).order_by(order)))


def _serialize(data: _In) -> dict:
    out = data.model_dump()
    for k, v in out.items():
        if hasattr(v, "unicode_string"):
            out[k] = str(v)
        elif v is not None and type(v).__name__ in ("HttpUrl", "Url", "AnyUrl"):
            out[k] = str(v)
    return out


def save_fact(session: Session, kind: str, data: _In, fact_id: int | None = None):
    model, _ = FACT_MODELS[kind]
    values = _serialize(data)
    if kind == "skills":
        norm = canonical(values["name"])
        values["normalized_name"] = norm
        values["category"] = values.get("category") or category_of(values["name"])
        existing = session.scalar(select(Skill).where(Skill.normalized_name == norm))
        if existing is not None and existing.id != fact_id:
            raise ValueError(f"Skill '{existing.name}' already exists")
    if fact_id is None:
        obj = model(**values)
        session.add(obj)
        action = "created"
    else:
        obj = session.get(model, fact_id)
        if obj is None:
            raise LookupError(f"{kind} #{fact_id} not found")
        for k, v in values.items():
            setattr(obj, k, v)
        action = "updated"
    if getattr(obj, "status", None) == "confirmed" and hasattr(obj, "confirmed_at") and obj.confirmed_at is None:
        obj.confirmed_at = utcnow()
    if getattr(obj, "status", None) != "confirmed" and hasattr(obj, "confirmed_at"):
        obj.confirmed_at = None
    session.flush()
    audit.record(session, f"fact.{action}", kind, obj.id, status=getattr(obj, "status", None))
    return obj


def set_fact_status(session: Session, kind: str, fact_id: int, status: str):
    model, _ = FACT_MODELS[kind]
    allowed = ("confirmed", "learning", "unverified") if kind == "skills" else ("confirmed", "unverified")
    if status not in allowed:
        raise ValueError(f"Invalid status {status!r}")
    obj = session.get(model, fact_id)
    if obj is None:
        raise LookupError(f"{kind} #{fact_id} not found")
    obj.status = status
    if hasattr(obj, "confirmed_at"):
        obj.confirmed_at = utcnow() if status == "confirmed" else None
    audit.record(session, f"fact.status.{status}", kind, fact_id)
    return obj


def delete_fact(session: Session, kind: str, fact_id: int) -> None:
    model, _ = FACT_MODELS[kind]
    obj = session.get(model, fact_id)
    if obj is not None:
        session.delete(obj)
        audit.record(session, "fact.deleted", kind, fact_id)


def suggest_skills_from_text(session: Session, text: str, source: str) -> list[Skill]:
    """Record skills detected in a resume's text as *unverified* suggestions.

    Existing skills are left untouched. The candidate must confirm them.
    """
    from jobpilot.services import skills_taxonomy
    from jobpilot.services.skills_taxonomy import extract_skills

    skills_taxonomy.configure_from_settings(session)
    created = []
    for name in extract_skills(text):
        if session.scalar(select(Skill).where(Skill.normalized_name == name)) is None:
            s = Skill(name=name, normalized_name=name, category=category_of(name), status="unverified",
                      source=source)
            session.add(s)
            created.append(s)
    if created:
        session.flush()
        audit.record(session, "fact.skills_suggested", "skills", None, count=len(created), source=source)
    return created


# ---- Read helpers used by matching / drafts / autofill ---------------------

def confirmed_skills(session: Session) -> set[str]:
    return set(session.scalars(select(Skill.normalized_name).where(Skill.status == "confirmed")))


def learning_skills(session: Session) -> set[str]:
    return set(session.scalars(select(Skill.normalized_name).where(Skill.status == "learning")))


def experience_years(session: Session) -> tuple[float | None, str]:
    """Candidate experience in years and how it was derived. ``None`` = unknown."""
    profile = get_profile(session)
    if profile.total_experience_years is not None:
        return profile.total_experience_years, "stated in profile"
    jobs = list(session.scalars(select(Employment).where(Employment.status == "confirmed")))
    full = [j for j in jobs if j.employment_type not in ("internship",) and j.start_date]
    if not full:
        if jobs:
            return None, "only internships or undated roles confirmed"
        return None, "no confirmed employment and no stated total"
    days = 0
    for j in full:
        end = j.end_date or date.today()
        days += max(0, (end - j.start_date).days)
    return round(days / 365.25, 1), f"computed from {len(full)} confirmed role(s)"


def completeness(session: Session) -> dict:
    p = get_profile(session)
    checks = {
        "name": bool(p.full_name or (p.first_name and p.last_name)),
        "email": bool(p.email),
        "location": bool(p.current_location or p.acceptable_locations),
        "experience": experience_years(session)[0] is not None,
        "confirmed skills": bool(confirmed_skills(session)),
        "education": bool(session.scalar(select(Education.id).limit(1))),
        "profile confirmed": p.confirmed_at is not None,
    }
    return {"checks": checks, "score": sum(checks.values()) / len(checks)}


def skill_profile(session: Session) -> dict[str, dict]:
    """Every recorded skill with its status and where it is evidenced in *confirmed* facts.

    ``work`` lists confirmed roles whose title/description mention the skill (internships labelled);
    ``projects`` lists confirmed personal projects. Nothing is inferred beyond those texts.
    """
    from jobpilot.services.skills_taxonomy import extract_skills

    jobs = list(session.scalars(select(Employment).where(Employment.status == "confirmed")))
    projects = list(session.scalars(select(Project).where(Project.status == "confirmed")))
    job_skills = {j.id: set(extract_skills(f"{j.title}\n{j.description or ''}")) for j in jobs}
    proj_skills = {p.id: set(extract_skills(f"{p.name}\n{p.description or ''}\n{', '.join(p.technologies or [])}"))
                   for p in projects}
    out: dict[str, dict] = {}
    for sk in session.scalars(select(Skill)):
        name = sk.normalized_name
        work = [f"{j.title} at {j.company}" + (" (internship)" if j.employment_type == "internship" else "")
                for j in jobs if name in job_skills[j.id]]
        projs = [p.name for p in projects if name in proj_skills[p.id]]
        out[name] = {"status": sk.status, "work": work, "projects": projs, "years": sk.years}
    return out
