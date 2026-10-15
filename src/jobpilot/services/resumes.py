"""Resume library: import, versioning, approval, generation and selection.

Guarantees:
- Imported files are stored byte-for-byte (sha256 recorded); never altered.
- Any new content (import or generation) creates a new **draft** version that
  must be approved before it can be selected for an application.
- Generated resumes use only *confirmed* profile facts, chosen explicitly,
  and record which fact ids were used.
- Selection never invents a resume: if no approved resume fits, it says so.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from html import escape
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from jobpilot.config import Settings
from jobpilot.models import Education, Employment, Job, Project, Resume, ResumeVersion, Skill, utcnow
from jobpilot.services import audit, profile as profile_svc, settings_store, storage
from jobpilot.services.skills_taxonomy import extract_skills

log = logging.getLogger(__name__)

STANDARD_SLOTS = [
    ("master", "Master resume", True),
    ("java_backend", "Java Backend", False),
    ("sde1", "SDE I", False),
    ("general_swe", "General SWE", False),
    ("python_backend", "Python Backend", False),
    ("ai_ml", "AI/ML", False),
]
FALLBACKS = {
    "sde1": ["general_swe", "java_backend", "python_backend"],
    "java_backend": ["general_swe", "sde1"],
    "python_backend": ["general_swe", "sde1"],
    "ai_ml": ["python_backend", "general_swe"],
    "general_swe": ["sde1", "java_backend", "python_backend"],
}
RESUME_EXTS = (".pdf", ".docx", ".txt", ".md")


class ResumeError(ValueError):
    pass


def list_resumes(session: Session) -> list[Resume]:
    return list(session.scalars(select(Resume).options(selectinload(Resume.versions))
                                .order_by(Resume.is_master.desc(), Resume.category, Resume.name)))


def create_resume(session: Session, name: str, category: str, is_master: bool = False,
                  description: str | None = None) -> Resume:
    name = (name or "").strip()
    if not name:
        raise ResumeError("Name is required")
    cats = settings_store.get(session, "role_categories")
    if category != "master" and category not in cats:
        raise ResumeError(f"Unknown category {category!r}")
    r = Resume(name=name[:200], category=category, is_master=is_master or category == "master",
               description=description)
    session.add(r)
    session.flush()
    audit.record(session, "resume.created", "resume", r.id, category=category)
    return r


def create_standard_slots(session: Session) -> int:
    existing = set(session.scalars(select(Resume.category)))
    n = 0
    for cat, label, master in STANDARD_SLOTS:
        if cat not in existing:
            create_resume(session, label, cat, master)
            n += 1
    return n


def extract_text(filename: str, data: bytes) -> dict[str, Any]:
    """Validate a resume file and extract its text."""
    ext = filename.lower().rsplit(".", 1)[-1]
    out: dict[str, Any] = {"ok": True, "errors": [], "warnings": [], "text": "", "page_count": None}
    try:
        if ext == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                out["ok"] = False
                out["errors"].append("PDF is password-protected")
                return out
            out["page_count"] = len(reader.pages)
            if out["page_count"] == 0:
                out["ok"] = False
                out["errors"].append("PDF has no pages")
            out["text"] = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
            if out["page_count"] and out["page_count"] > 3:
                out["warnings"].append(f"{out['page_count']} pages — most resumes are 1–2 pages")
        elif ext == "docx":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            xml = re.sub(r"</w:p>", "\n", xml)
            out["text"] = re.sub(r"<[^>]+>", "", xml)
            out["text"] = re.sub(r"[ \t]+", " ", out["text"]).strip()
        else:
            out["text"] = data.decode("utf-8").strip()
    except Exception as exc:  # malformed files
        out["ok"] = False
        out["errors"].append(f"Could not read file: {type(exc).__name__}: {exc}")
        return out
    if not out["text"]:
        out["warnings"].append("No extractable text (scanned image?). Selection by skills will be limited.")
    out["text"] = out["text"][:200_000]
    return out


def _next_version_no(session: Session, resume_id: int) -> int:
    return (session.scalar(select(func.max(ResumeVersion.version_no)).where(ResumeVersion.resume_id == resume_id)) or 0) + 1


def import_version(session: Session, settings: Settings, resume: Resume, filename: str, data: bytes,
                   note: str | None = None, change_summary: str | None = None) -> ResumeVersion:
    stored = storage.store(settings, "resumes", filename, data, allowed=RESUME_EXTS)
    dup = session.scalar(select(ResumeVersion).where(ResumeVersion.resume_id == resume.id,
                                                     ResumeVersion.sha256 == stored.sha256))
    if dup is not None:
        raise ResumeError(f"This exact file is already version {dup.version_no} of '{resume.name}'")
    v = extract_text(filename, data)
    if not v["ok"]:
        raise ResumeError("; ".join(v["errors"]))
    version = ResumeVersion(
        resume_id=resume.id, version_no=_next_version_no(session, resume.id), status="draft", source="imported",
        source_note=note or f"uploaded file {stored.original_name}", change_summary=change_summary,
        file_path=stored.rel_path, original_filename=stored.original_name, mime_type=stored.mime_type,
        file_size=stored.size, sha256=stored.sha256, page_count=v["page_count"], extracted_text=v["text"],
        validation={"errors": v["errors"], "warnings": v["warnings"],
                    "skills_detected": extract_skills(v["text"])},
        created_at=utcnow(),
    )
    session.add(version)
    session.flush()
    audit.record(session, "resume.version.imported", "resume_version", version.id, resume_id=resume.id,
                 sha256=stored.sha256)
    return version


def approve_version(session: Session, version: ResumeVersion) -> ResumeVersion:
    if version.status == "approved":
        return version
    if version.validation and version.validation.get("errors"):
        raise ResumeError("Version has validation errors and cannot be approved")
    version.status = "approved"
    version.approved_at = utcnow()
    audit.record(session, "resume.version.approved", "resume_version", version.id, resume_id=version.resume_id)
    return version


def archive_version(session: Session, version: ResumeVersion) -> ResumeVersion:
    version.status = "archived"
    audit.record(session, "resume.version.archived", "resume_version", version.id)
    return version


# ---- Generation from confirmed facts -----------------------------------------

def _fmt_range(start, end) -> str:
    s = start.strftime("%b %Y") if start else ""
    e = end.strftime("%b %Y") if end else ("Present" if start else "")
    return f"{s} – {e}".strip(" –")


def build_resume_html(session: Session, selection: dict[str, list[int]]) -> tuple[str, dict]:
    """Render a resume from explicitly selected, **confirmed** facts only."""
    p = profile_svc.get_profile(session)
    if not (p.full_name or (p.first_name and p.last_name)):
        raise ResumeError("Profile name is required to generate a resume")
    if p.confirmed_at is None:
        raise ResumeError("Confirm your profile details before generating a resume")

    def pick(model, ids):
        if not ids:
            return []
        rows = list(session.scalars(select(model).where(model.id.in_(ids))))
        bad = [r.id for r in rows if r.status != "confirmed"]
        if bad:
            raise ResumeError(f"Only confirmed facts can be used; unconfirmed {model.__tablename__} ids: {bad}")
        return rows

    jobs = sorted(pick(Employment, selection.get("employment")), key=lambda e: e.start_date or utcnow().date(), reverse=True)
    projects = pick(Project, selection.get("projects"))
    edu = pick(Education, selection.get("education"))
    skills = pick(Skill, selection.get("skills"))
    name = p.full_name or f"{p.first_name} {p.last_name}"
    contact = " · ".join(escape(x) for x in (p.email, p.phone, p.current_location) if x)

    def para(text: str | None) -> str:
        if not text:
            return ""
        lines = [line.strip("•-* ").strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1:
            return "<ul>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + "</ul>"
        return f"<p>{escape(lines[0])}</p>" if lines else ""

    parts = [f"<h1>{escape(name)}</h1>"]
    if p.headline:
        parts.append(f"<div class='headline'>{escape(p.headline)}</div>")
    if contact:
        parts.append(f"<div class='contact'>{contact}</div>")
    if p.summary:
        parts.append(f"<h2>Summary</h2>{para(p.summary)}")
    if skills:
        parts.append("<h2>Skills</h2><p>" + ", ".join(escape(s.name) for s in skills) + "</p>")
    if jobs:
        parts.append("<h2>Experience</h2>")
        for j in jobs:
            parts.append(f"<div class='item'><div class='row'><b>{escape(j.title)}</b>, {escape(j.company)}"
                         f"<span>{escape(_fmt_range(j.start_date, j.end_date))}</span></div>{para(j.description)}</div>")
    if projects:
        parts.append("<h2>Projects</h2>")
        for pr in projects:
            tech = f" — {escape(', '.join(pr.technologies))}" if pr.technologies else ""
            parts.append(f"<div class='item'><b>{escape(pr.name)}</b>{tech}{para(pr.description)}</div>")
    if edu:
        parts.append("<h2>Education</h2>")
        for e in edu:
            deg = ", ".join(x for x in (e.degree, e.field_of_study) if x)
            parts.append(f"<div class='item'><div class='row'><b>{escape(e.institution)}</b>"
                         f"<span>{escape(_fmt_range(e.start_date, e.end_date))}</span></div>"
                         f"{escape(deg)}{(' · ' + escape(e.grade)) if e.grade else ''}</div>")
    css = ("body{font-family:Helvetica,Arial,sans-serif;font-size:10.5pt;color:#111;margin:0}"
           "h1{font-size:20pt;margin:0}h2{font-size:11.5pt;border-bottom:1px solid #444;margin:14px 0 6px;"
           "text-transform:uppercase;letter-spacing:.05em}.headline{font-size:11pt;margin-top:2px}"
           ".contact{color:#333;margin-top:4px}.item{margin-bottom:8px}.row{display:flex;justify-content:space-between}"
           "ul{margin:3px 0 0 18px;padding:0}li{margin:1px 0}p{margin:3px 0}")
    html = f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>{''.join(parts)}</body></html>"
    content = {"profile_confirmed_at": p.confirmed_at.isoformat(), "employment": [j.id for j in jobs],
               "projects": [x.id for x in projects], "education": [e.id for e in edu], "skills": [s.id for s in skills],
               "expected_strings": [name] + [j.company for j in jobs] + [x.institution for x in edu]}
    return html, content


def generate_version(session: Session, settings: Settings, resume: Resume, selection: dict[str, list[int]],
                     change_summary: str | None = None) -> ResumeVersion:
    from jobpilot.automation.pdf import PdfUnavailable, html_to_pdf

    html, content = build_resume_html(session, selection)
    try:
        data, filename = html_to_pdf(html, settings), f"resume-{resume.category}.pdf"
    except PdfUnavailable as exc:
        log.info("pdf generation unavailable, storing HTML", extra={"reason": str(exc)})
        data, filename = html.encode("utf-8"), f"resume-{resume.category}.html"
    stored = storage.store(settings, "resumes", filename, data)
    if filename.endswith(".pdf"):
        v = extract_text(filename, data)
    else:
        v = {"ok": True, "errors": [], "warnings": ["PDF generation unavailable; stored as HTML (print to PDF from browser)"],
             "text": re.sub(r"<[^>]+>", " ", html), "page_count": None}
    missing = [s for s in content["expected_strings"] if s and s.lower() not in re.sub(r"\s+", " ", v["text"]).lower()]
    if missing:
        v["errors"].append(f"Generated file is missing expected text: {', '.join(missing)}")
    version = ResumeVersion(
        resume_id=resume.id, version_no=_next_version_no(session, resume.id), status="draft", source="generated",
        source_note="generated from confirmed profile facts", change_summary=change_summary,
        file_path=stored.rel_path, original_filename=filename, mime_type=stored.mime_type, file_size=stored.size,
        sha256=stored.sha256, page_count=v["page_count"], extracted_text=v["text"], content=content,
        validation={"errors": v["errors"], "warnings": v["warnings"], "skills_detected": extract_skills(v["text"])},
        created_at=utcnow(),
    )
    session.add(version)
    session.flush()
    audit.record(session, "resume.version.generated", "resume_version", version.id, resume_id=resume.id)
    return version


# ---- Selection --------------------------------------------------------------

def select_for_job(session: Session, job: Job) -> dict[str, Any]:
    """Choose the most suitable *approved* resume version for a job, with reasons."""
    category = job.category
    details = job.match_details or {}
    job_skills = set((details.get("skills") or {}).get("mandatory", [])) | set((details.get("skills") or {}).get("preferred", []))
    resumes = [r for r in list_resumes(session) if r.active and r.latest_approved]
    if not resumes:
        return {"version": None, "missing": True, "fallback": False, "alternatives": [],
                "reason": "No approved resume exists. Import a resume and approve it."}

    def coverage(r: Resume) -> tuple[int, list[str]]:
        detected = set((r.latest_approved.validation or {}).get("skills_detected") or [])
        hits = sorted(job_skills & detected)
        return len(hits), hits

    def best_of(cands: list[Resume]) -> Resume:
        return max(cands, key=lambda r: (coverage(r)[0], r.latest_approved.version_no))

    chosen, reason, fallback = None, "", False
    if category:
        exact = [r for r in resumes if r.category == category]
        if exact:
            chosen = best_of(exact)
            reason = f"matches job category '{category}'"
        else:
            for fb in FALLBACKS.get(category, []):
                cands = [r for r in resumes if r.category == fb]
                if cands:
                    chosen, fallback = best_of(cands), True
                    reason = f"no approved '{category}' resume; using closest category '{fb}'"
                    break
    if chosen is None:
        masters = [r for r in resumes if r.is_master]
        if masters:
            chosen, fallback = best_of(masters), True
            reason = ("job category unknown; " if not category else f"no approved resume for '{category}'; ") + "using master resume"
        else:
            chosen, fallback = best_of(resumes), True
            reason = ("job category unknown" if not category else f"no approved resume for '{category}' or related") \
                + "; chose the approved resume covering the most job skills"
    n, hits = coverage(chosen)
    if job_skills:
        reason += f"; covers {n}/{len(job_skills)} job skills" + (f" ({', '.join(hits)})" if hits else "")
    alts = [{"resume": r.name, "version_id": r.latest_approved.id, "category": r.category, "coverage": coverage(r)[0]}
            for r in resumes if r.id != chosen.id]
    return {"version": chosen.latest_approved, "missing": False, "fallback": fallback, "reason": reason,
            "alternatives": sorted(alts, key=lambda a: -a["coverage"])}
