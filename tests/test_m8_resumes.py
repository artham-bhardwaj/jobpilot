"""Milestone 8 — Stage E: resume selection on verified facts, warnings, review view (offline; synthetic)."""

from __future__ import annotations

from sqlalchemy import select

from jobpilot.models import Resume, ResumeVersion
from jobpilot.services import jobs as job_svc
from jobpilot.services import profile as ps
from jobpilot.services import resumes as rs
from tests.helpers import make_pdf_bytes, seed_profile


def _resume(db, settings, category, lines, approve=True):
    r = db.scalar(select(Resume).where(Resume.category == category))
    v = rs.import_version(db, settings, r, f"{category}.pdf", make_pdf_bytes(lines))
    if approve:
        rs.approve_version(db, v)
    db.flush()
    db.expire_all()
    return v


def _job(db, title, desc):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name="Examplecorp", description_text=desc))
    return job


def test_selection_uses_verified_skills_and_explains(db, settings):
    seed_profile(db, skills=("java", "spring boot", "sql"))
    rs.create_standard_slots(db)
    v = _resume(db, settings, "java_backend", ["Test Candidate", "Java Spring Boot SQL Kafka"])
    job = _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- Spring Boot\n- SQL\n- Kafka\n- Redis")
    sel = rs.select_for_job(db, job)
    assert sel["version"].id == v.id and not sel["fallback"] and not sel["missing"]
    assert sel["coverage"]["required"] == ["java", "spring boot", "sql"], "kafka is in the resume but not confirmed"
    assert any("mentions kafka, which your profile does not confirm" in w for w in sel["warnings"])
    assert any("category matches" in r for r in sel["reasons"])


def test_confirmed_skill_missing_from_resume_is_flagged(db, settings):
    seed_profile(db, skills=("java", "sql", "docker"), learning=())
    rs.create_standard_slots(db)
    _resume(db, settings, "java_backend", ["Test Candidate", "Java SQL"])
    sel = rs.select_for_job(db, _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- SQL\n- Docker"))
    assert sel["not_in_resume"] == ["docker"]
    assert any("doesn't mention" in w and "won't edit" in w for w in sel["warnings"])


def test_best_of_several_same_category_by_verified_coverage(db, settings):
    seed_profile(db, skills=("python", "django", "postgresql"))
    a = rs.create_resume(db, "Python A", "python_backend")
    b = rs.create_resume(db, "Python B", "python_backend")
    va = rs.import_version(db, settings, a, "a.pdf", make_pdf_bytes(["Test Candidate", "Python"]))
    vb = rs.import_version(db, settings, b, "b.pdf", make_pdf_bytes(["Test Candidate", "Python Django PostgreSQL"]))
    rs.approve_version(db, va)
    rs.approve_version(db, vb)
    db.flush()
    db.expire_all()
    sel = rs.select_for_job(db, _job(db, "Python Backend Developer", "Requirements:\n- Python\n- Django\n- PostgreSQL"))
    assert sel["version"].id == vb.id and "chosen among 2" in sel["reason"]


def test_missing_and_unsuitable_variants_flagged(db, settings):
    seed_profile(db, skills=("python",), learning=())
    rs.create_standard_slots(db)
    sel = rs.select_for_job(db, _job(db, "Machine Learning Engineer", "Requirements:\n- PyTorch\n- Python\n- NLP"))
    assert sel["missing"] and sel["version"] is None and sel["warnings"]
    _resume(db, settings, "master", ["Test Candidate", "Java"])
    sel = rs.select_for_job(db, _job(db, "ML Engineer", "Requirements:\n- PyTorch\n- Python\n- NLP\n- Machine learning"))
    assert sel["fallback"] and sel["unsuitable"]
    assert any("less than half of the required skills" in w for w in sel["warnings"])
    assert any("no resume variant for" in w for w in sel["warnings"])


def test_draft_versions_are_never_selected_and_approved_never_replaced(db, settings):
    seed_profile(db)
    rs.create_standard_slots(db)
    v1 = _resume(db, settings, "java_backend", ["Test Candidate", "Java"])
    v2 = _resume(db, settings, "java_backend", ["Test Candidate", "Java Spring Boot SQL"], approve=False)
    sel = rs.select_for_job(db, _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- Spring Boot\n- SQL"))
    assert sel["version"].id == v1.id, "a newer draft never replaces the approved version"
    assert db.get(ResumeVersion, v2.id).status == "draft" and db.get(ResumeVersion, v1.id).sha256


def test_resume_review_page(client, db, settings):
    seed_profile(db)
    ps.save_fact(db, "projects", ps.ProjectIn(name="Inventory API", technologies="Java, SQL", status="confirmed"))
    rs.create_standard_slots(db)
    _resume(db, settings, "java_backend", ["Test Candidate", "Java Spring Boot"])
    job = _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- Spring Boot\n- SQL")
    db.commit()
    page = client.get(f"/jobs/{job.id}/resume-review").text
    for text in ("Resume selection review", "Java Backend v1", "Why", "Relevant confirmed experience",
                 "Inventory API", "personal project", "requirements section"):
        assert text in page, text
    assert client.get("/jobs/999999/resume-review").status_code == 404
    page2 = client.get(f"/jobs/{job.id}").text
    assert f"/jobs/{job.id}/resume-review" in page2
