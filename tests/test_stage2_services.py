"""Stage 2: profile facts, parsing, sources, dedup/repost/expiry, matching, resumes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from jobpilot.models import Job, JobSource, Resume
from jobpilot.services import jobs as job_svc
from jobpilot.services import matching, profile as ps, resumes as rs
from jobpilot.sources.base import JobData
from tests.helpers import fixture_text, make_http, make_pdf_bytes, seed_profile

GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"
LEVER = "https://api.lever.co/v0/postings/fictionallabs"


# ---- Profile -------------------------------------------------------------

def test_profile_optional_and_validated(db):
    p = ps.get_profile(db)
    assert p.full_name is None  # nothing required at setup
    with pytest.raises(ValidationError):
        ps.ProfileIn(email="not-an-email")
    ps.update_profile(db, ps.ProfileIn(full_name="Test Candidate", acceptable_locations="Pune, Remote"))
    assert ps.get_profile(db).acceptable_locations == ["Pune", "Remote"]
    assert ps.get_profile(db).confirmed_at is None
    ps.confirm_profile(db)
    ps.update_profile(db, ps.ProfileIn(full_name="Test Candidate 2"))
    assert ps.get_profile(db).confirmed_at is None, "edits must require re-confirmation"


def test_skill_statuses_and_dedup(db):
    ps.save_fact(db, "skills", ps.SkillIn(name="Spring-Boot", status="confirmed"))
    with pytest.raises(ValueError):
        ps.save_fact(db, "skills", ps.SkillIn(name="springboot"))
    ps.save_fact(db, "skills", ps.SkillIn(name="Kafka", status="learning"))
    assert ps.confirmed_skills(db) == {"spring boot"}
    assert ps.learning_skills(db) == {"kafka"}


def test_suggested_skills_are_unverified(db):
    created = ps.suggest_skills_from_text(db, "Experienced with Java, Docker and PostgreSQL", "resume v1")
    assert {s.normalized_name for s in created} == {"java", "docker", "postgresql"}
    assert all(s.status == "unverified" for s in created)
    assert ps.confirmed_skills(db) == set()


def test_experience_unknown_without_facts(db):
    years, why = ps.experience_years(db)
    assert years is None and "no confirmed" in why


# ---- Sources & ingestion -------------------------------------------------------

def test_greenhouse_and_lever_collection(db, settings):
    seed_profile(db)
    gh = job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                                  config={"board_token": "examplecorp"}))
    lv = job_svc.save_source(db, job_svc.SourceIn(name="Fictional Labs", kind="lever", company_name="Fictional Labs",
                                                  config={"site": "fictionallabs"}))
    http = make_http(settings, {GH: (200, fixture_text("greenhouse_jobs.json")),
                                LEVER: (200, fixture_text("lever_postings.json"))})
    summary = job_svc.collect_all(db, settings, http)
    assert summary["failed"] == 0 and summary["new"] == 3
    java = db.scalar(select(Job).where(Job.external_id == "1001"))
    assert java.company_name == "Examplecorp" and java.remote_type == "hybrid"
    assert java.experience_min_years == 1 and java.posted_at is not None
    assert java.sightings and java.sightings[0].method == "api"
    py = db.scalar(select(Job).where(Job.source_id == lv.id))
    assert py.remote_type == "remote" and py.salary_min == 1500000 and py.salary_currency == "INR"
    assert py.match_details["requirements"]["sponsorship"] == "not_available"
    assert gh.last_status == "success"


def test_rerun_is_idempotent_and_expires_missing(db, settings):
    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", config={"board_token": "examplecorp"},
                                             company_name="Examplecorp"))
    http = make_http(settings, {GH: (200, fixture_text("greenhouse_jobs.json"))})
    job_svc.collect_all(db, settings, http)
    again = job_svc.collect_all(db, settings, http)
    assert again["new"] == 0
    assert again["sources"]["Examplecorp"]["unchanged"] == 2
    # Job 1002 disappears from the board -> expired.
    only_first = '{"jobs": [' + fixture_text("greenhouse_jobs.json").split('{"jobs": [')[1].split("},\n  {")[0] + "}]}"
    http2 = make_http(settings, {GH: (200, only_first)})
    third = job_svc.collect_all(db, settings, http2)
    assert third["sources"]["Examplecorp"]["expired"] == 1
    assert db.scalar(select(Job).where(Job.external_id == "1002")).status == "expired"


def test_source_failure_is_recorded_not_fatal(db, settings):
    job_svc.save_source(db, job_svc.SourceIn(name="Broken", kind="lever", config={"site": "nobody"}))
    http = make_http(settings, {"https://api.lever.co/v0/postings/nobody": (500, "boom")})
    summary = job_svc.collect_all(db, settings, http)
    assert summary["failed"] == 1
    src = db.scalar(select(JobSource).where(JobSource.name == "Broken"))
    assert src.last_status == "failed" and "500" in src.last_error


def test_source_config_validation(db):
    with pytest.raises(ValueError):
        job_svc.save_source(db, job_svc.SourceIn(name="Bad", kind="greenhouse", config={"board_token": "../etc"}))
    with pytest.raises(ValueError):
        job_svc.save_source(db, job_svc.SourceIn(name="Bad", kind="nope", config={}))


def test_jsonld_url_and_robots(db, settings):
    url = "https://careers.example.net/jobs/sa-77"
    http = make_http(settings, {url: (200, fixture_text("career_page.html"))})
    job, outcome, warnings = job_svc.add_from_url(db, http, url)
    assert outcome == "new" and not warnings
    assert job.title == "Machine Learning Engineer" and job.company_name == "Sample Analytics"
    assert job.location == "Pune, MH, IN" and job.salary_max == 1600000 and job.external_id == "SA-77"
    assert job.category == "ai_ml"
    # robots.txt is honoured when enabled
    settings.respect_robots_txt = True
    http2 = make_http(settings, {"https://blocked.example.net/robots.txt": (200, "User-agent: *\nDisallow: /"),
                                 "https://blocked.example.net/jobs/1": (200, "<h1>x</h1>")})
    from jobpilot.sources.http import BlockedByRobots

    with pytest.raises(BlockedByRobots):
        job_svc.add_from_url(db, http2, "https://blocked.example.net/jobs/1")


def test_greenhouse_url_uses_api(db, settings):
    single = '{"id": 1001, "title": "Software Engineer I", "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/1001", "location": {"name": "Remote"}, "content": "Java"}'
    http = make_http(settings, {f"{GH}/1001": (200, single)})
    job, outcome, _ = job_svc.add_from_url(db, http, "https://boards.greenhouse.io/examplecorp/jobs/1001", "Examplecorp")
    assert job.external_id == "1001" and job.sightings[0].method == "api"


def test_csv_import_and_dedup(db):
    content = fixture_text("jobs_import.csv").encode()
    stats = job_svc.import_file(db, "jobs.csv", content)
    assert stats["new"] == 2 and len(stats["errors"]) == 1
    again = job_svc.import_file(db, "jobs.csv", content)
    assert again["new"] == 0 and again["unchanged"] == 2


def test_cross_source_dedup_and_repost(db):
    src_a = job_svc.get_or_create_source(db, "A", "manual")
    src_b = job_svc.get_or_create_source(db, "B", "manual")
    data = JobData(title="Backend Engineer", company_name="Demo Widgets Pvt Ltd", location="Pune",
                   description_text="Java", external_id="x1")
    j1, o1 = job_svc.upsert(db, src_a, data, "manual")
    same = JobData(title="Backend Engineer (Remote)", company_name="Demo Widgets", location="Pune",
                   description_text="Java", external_id="zz")
    j2, o2 = job_svc.upsert(db, src_b, same, "manual")
    assert o1 == "new" and o2 in ("unchanged", "updated") and j1.id == j2.id
    assert len(j1.sightings) == 2
    job_svc.mark_expired(db, j1, "test")
    j3, o3 = job_svc.upsert(db, src_b, JobData(title="Backend Engineer", company_name="Demo Widgets",
                                                location="Pune", external_id="new-id"), "manual")
    assert o3 == "repost" and j3.repost_of_id == j1.id


# ---- Matching ------------------------------------------------------------------

def _job(db, title, desc, location="Bengaluru", remote=None):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name="Examplecorp", location=location,
                                                        description_text=desc, remote_type=remote))
    return job


def test_match_explained_and_eligibility(db):
    seed_profile(db)
    job = _job(db, "Software Engineer I - Java Backend",
               "Requirements:\n- 1+ years of experience\n- Java, Spring Boot, SQL\nNice to have:\n- Kafka", remote="hybrid")
    d = job.match_details
    assert job.category == "java_backend"
    assert job.relevance_score >= 70
    assert {c["name"] for c in d["relevance"]["components"]} == {"role fit", "skills", "level", "location", "keywords", "salary"}
    assert all(c["explanation"] for c in d["relevance"]["components"])
    assert d["skills"]["missing_mandatory"] == [] and d["skills"]["learning"] == ["kafka"]
    assert job.eligibility in ("eligible", "likely_eligible")


def test_unknown_requirements_never_eligible(db):
    seed_profile(db)
    job = _job(db, "Backend Engineer", "Great team, great snacks.")
    assert job.eligibility == "unknown"
    assert job.match_details["missing_info"]


def test_unknown_candidate_experience_is_unknown(db):
    seed_profile(db, years=None)
    from jobpilot.models import Employment

    for e in db.scalars(select(Employment)):
        db.delete(e)
    db.flush()
    job = _job(db, "Java Developer", "Requirements: 2+ years of experience with Java and SQL")
    exp = next(c for c in job.match_details["eligibility"]["checks"] if c["name"] == "experience")
    assert exp["status"] == "unknown" and job.eligibility == "unknown"


def test_ineligible_and_exclusions(db):
    seed_profile(db, needs_sponsorship=True)
    senior = _job(db, "Principal Engineer", "Requirements: 10+ years of experience with Java")
    assert senior.relevance_score == 0 and senior.match_details["relevance"]["excluded_by"]
    assert senior.eligibility == "ineligible"
    nosponsor = _job(db, "Java Developer", "Requirements: Java, SQL. 1+ years of experience. No visa sponsorship.",
                     remote="remote")
    assert nosponsor.eligibility == "ineligible"


def test_review_flags(db):
    """Relevant job + unknown eligibility (candidate experience unknown) -> must be queued for review."""
    seed_profile(db, years=None)
    from jobpilot.models import Employment

    for e in db.scalars(select(Employment)):
        db.delete(e)
    db.flush()
    job = _job(db, "Java Backend Engineer", "Requirements: Java, Spring Boot, SQL. 2+ years of experience.", remote="hybrid")
    assert job.relevance_score >= 55 and job.eligibility == "unknown"
    assert job.match_details["needs_review"] is True
    assert "eligibility could not be determined" in " ".join(job.match_details["review_reasons"])
    low = _job(db, "Office Manager", "Requirements: scheduling, MS Office.")
    assert low.match_details["needs_review"] is False


# ---- Resumes -------------------------------------------------------------------

def test_resume_import_approve_select(db, settings):
    seed_profile(db)
    rs.create_standard_slots(db)
    java = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    master = db.scalar(select(Resume).where(Resume.category == "master"))
    pdf = make_pdf_bytes(["Test Candidate", "Java Spring Boot SQL REST APIs"])
    v = rs.import_version(db, settings, java, "java.pdf", pdf)
    assert v.status == "draft" and v.page_count == 1 and "java" in v.validation["skills_detected"]
    with pytest.raises(rs.ResumeError):
        rs.import_version(db, settings, java, "java.pdf", pdf)  # identical file
    with pytest.raises(Exception):
        rs.import_version(db, settings, java, "fake.pdf", b"not a pdf")

    job = _job(db, "Java Backend Engineer", "Requirements: Java, Spring Boot, SQL. 1+ years of experience")
    sel = rs.select_for_job(db, job)
    assert sel["missing"], "draft versions must not be selectable"

    rs.approve_version(db, v)
    db.flush()
    db.expire_all()
    sel = rs.select_for_job(db, job)
    assert sel["version"].id == v.id and not sel["fallback"] and "covers" in sel["reason"]

    py_job = _job(db, "Python Backend Developer", "Requirements: Python, Django")
    mv = rs.import_version(db, settings, master, "master.txt", b"Test Candidate\nPython Java")
    rs.approve_version(db, mv)
    db.flush()
    db.expire_all()
    sel = rs.select_for_job(db, py_job)
    assert sel["fallback"] is True


def test_generated_resume_uses_only_confirmed_facts(db, settings, monkeypatch):
    from jobpilot.automation import pdf as pdfmod
    from jobpilot.models import Employment, Skill

    seed_profile(db)
    ps.save_fact(db, "skills", ps.SkillIn(name="Rust", status="unverified"))
    db.flush()
    r = rs.create_resume(db, "Generated", "java_backend")
    unverified = db.scalar(select(Skill).where(Skill.normalized_name == "rust"))
    with pytest.raises(rs.ResumeError):
        rs.build_resume_html(db, {"skills": [unverified.id]})

    monkeypatch.setattr(pdfmod, "html_to_pdf", lambda html, s: (_ for _ in ()).throw(pdfmod.PdfUnavailable("test")))
    emp = db.scalar(select(Employment))
    skills = [s.id for s in db.scalars(select(Skill).where(Skill.status == "confirmed"))]
    v = rs.generate_version(db, settings, r, {"employment": [emp.id], "skills": skills})
    assert v.status == "draft" and v.source == "generated"
    assert v.content["employment"] == [emp.id]
    assert not v.validation["errors"]


def test_alternative_skills_count_once(db):
    seed_profile(db, skills=("python", "fastapi", "postgresql"))
    job = _job(db, "Python Backend Developer",
               "Requirements:\n- Python\n- Django or FastAPI\n- PostgreSQL\n- 1+ years of experience")
    skills_check = next(c for c in job.match_details["eligibility"]["checks"] if c["name"] == "required skills")
    assert skills_check["status"] == "pass", skills_check
    assert job.match_details["skills"]["missing_mandatory"] == []


def test_degree_regex_requires_word_boundary():
    from jobpilot.services import job_parser as jp

    assert jp.parse("x", "Fictional Labs builds data tooling.").degree_required is None
    assert "Bachelor" in jp.parse("x", "Requirements: Bachelor's degree in CS").degree_required


def test_real_pdf_generation_when_chrome_available(settings):
    """Environment-dependent: uses the locally installed Chrome via Playwright; skipped if unavailable."""
    from jobpilot.automation.pdf import PdfUnavailable, html_to_pdf

    try:
        pdf = html_to_pdf("<h1>Test Candidate</h1><p>Synthetic Corp</p>", settings)
    except PdfUnavailable as exc:
        pytest.skip(f"PDF rendering unavailable: {exc}")
    v = rs.extract_text("x.pdf", pdf)
    assert v["ok"] and v["page_count"] == 1 and "Test Candidate" in v["text"]
