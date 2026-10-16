"""Milestone 11: truthful candidate profile — certifications, achievements, project kinds, kind of work, outdated facts.

Synthetic data only.
"""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from jobpilot.models import Achievement, Certification, Skill
from jobpilot.services import jobs as job_svc
from jobpilot.services import matching
from jobpilot.services import profile as ps
from tests.helpers import seed_profile


def _job(db, title, desc):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name="Examplecorp", location="Bengaluru",
                                                        description_text=desc))
    return job


def _experience_check(job):
    return next(c for c in job.match_details["eligibility"]["checks"] if c["name"] == "experience")


# ---- New fact kinds and states ------------------------------------------------------------

def test_certifications_and_achievements_follow_the_fact_workflow(db):
    cert = ps.save_fact(db, "certifications", ps.CertificationIn(name="Synthetic Cloud Practitioner", issuer="Example Org",
                                                                 issued_on=date(2025, 1, 10)))
    ach = ps.save_fact(db, "achievements", ps.AchievementIn(title="Won a college hackathon (synthetic)", context="academic"))
    assert cert.status == "unverified" and cert.confirmed_at is None, "new facts are never verified by default"
    ps.set_fact_status(db, "certifications", cert.id, "confirmed")
    assert cert.status == "confirmed" and cert.confirmed_at is not None
    ps.set_fact_status(db, "achievements", ach.id, "outdated")
    assert ach.status == "outdated" and ach.confirmed_at is None
    assert db.scalar(select(Certification)).name == "Synthetic Cloud Practitioner"
    assert db.scalar(select(Achievement)).context == "academic"


@pytest.mark.parametrize("kind,data", [
    ("certifications", {"name": ""}),
    ("certifications", {"name": "X", "credential_url": "javascript:alert(1)"}),
    ("achievements", {"title": "X", "context": "production"}),
    ("projects", {"name": "X", "kind": "professional"}),
    ("employment", {"company": "X", "title": "Y", "role_type": "backend_ninja"}),
])
def test_invalid_fact_values_are_rejected(kind, data):
    _, schema = ps.FACT_MODELS[kind]
    with pytest.raises(ValidationError):
        schema.model_validate(data)


def test_outdated_skill_is_never_used_as_a_confirmed_skill(db):
    seed_profile(db, skills=("java", "sql"), learning=())
    sql = db.scalar(select(Skill).where(Skill.normalized_name == "sql"))
    ps.set_fact_status(db, "skills", sql.id, "outdated")
    assert ps.confirmed_skills(db) == {"java"}
    job = _job(db, "Java Developer", "Requirements:\n- Java\n- SQL")
    assert "sql" in job.match_details["skills"]["missing_mandatory"]
    with pytest.raises(ValueError):
        ps.set_fact_status(db, "skills", sql.id, "verified-by-magic")


# ---- Projects are never work; their kind is kept ------------------------------------------

def test_academic_and_learning_projects_are_labelled_and_not_work(db):
    seed_profile(db, skills=("java", "kafka", "docker"), learning=())
    ps.save_fact(db, "projects", ps.ProjectIn(name="Course assignment", kind="academic", technologies="Kafka",
                                              status="confirmed"))
    ps.save_fact(db, "projects", ps.ProjectIn(name="Docker tutorial", kind="learning_exercise", technologies="Docker",
                                              status="confirmed"))
    prof = ps.skill_profile(db)
    assert prof["kafka"]["projects"] == [] and prof["kafka"]["non_work"] == ["Course assignment (academic)"]
    assert prof["docker"]["non_work"] == ["Docker tutorial (learning exercise)"] and prof["docker"]["work"] == []
    job = _job(db, "Java Developer", "Requirements:\n- Java\n- Kafka\n- Docker")
    note = job.match_details["skills"]["candidate"]["kafka"]
    assert "academic/learning (not work): Course assignment (academic)" in note and "work:" not in note.replace("(not work)", "")


# ---- Kind of work: testing years are never presented as development years ----------------

def _testing_career(db, role_type="testing_qa"):
    seed_profile(db, years=None, skills=("java", "sql"), learning=())
    for e in ps.list_facts(db, "employment"):
        ps.delete_fact(db, "employment", e.id)
    ps.save_fact(db, "employment", ps.EmploymentIn(
        company="Synthetic IT Services", title="Systems Engineer", employment_type="full_time", role_type=role_type,
        start_date=date(2021, 1, 1), end_date=date(2025, 1, 1), description="Integration testing (synthetic).",
        status="confirmed"))
    db.flush()


def test_testing_experience_does_not_pass_a_development_years_requirement(db):
    _testing_career(db)
    assert ps.experience_breakdown(db)["by_type"] == {"testing_qa": 4.0}
    job = _job(db, "Backend Software Engineer", "Requirements:\n- 3+ years of experience with Java\n- SQL")
    check = _experience_check(job)
    assert check["status"] == "warn"
    assert "of which 0 in software development roles" in check["explanation"] and "4 testing qa" in check["explanation"]


def test_testing_experience_counts_fully_for_a_testing_role(db):
    _testing_career(db)
    job = _job(db, "QA Automation Engineer", "Requirements:\n- 3+ years of experience with Java\n- SQL")
    assert _experience_check(job)["status"] == "pass"


def test_development_role_with_development_experience_passes(db):
    _testing_career(db, role_type="software_development")
    job = _job(db, "Backend Software Engineer", "Requirements:\n- 3+ years of experience with Java\n- SQL")
    assert _experience_check(job)["status"] == "pass"


def test_unrecorded_kind_of_work_is_flagged_not_guessed(db):
    _testing_career(db, role_type=None)
    job = _job(db, "Backend Software Engineer", "Requirements:\n- 3+ years of experience with Java\n- SQL")
    check = _experience_check(job)
    assert check["status"] == "warn" and "kind of work not recorded for 1 role(s)" in check["explanation"]
    assert any("kind of work" in m for m in job.match_details["missing_profile_info"])


@pytest.mark.parametrize("title,dev", [("Backend Software Engineer", True), ("SDE I", True), ("Java Developer", True),
                                       ("QA Engineer", False), ("SDET", False), ("Test Automation Engineer", False),
                                       ("Product Manager", False)])
def test_development_role_detection(title, dev):
    assert matching.is_development_role(title) is dev


# ---- Web and migration --------------------------------------------------------------------

def test_profile_page_shows_and_saves_new_sections(client, db):
    page = client.get("/profile").text
    for heading in ("Certifications", "Achievements", "Preferred job titles", "Kind of work", "never counted as work"):
        assert heading in page, heading
    r = client.post("/profile/certifications", data={"name": "Synthetic Cert", "status": "unverified"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    r = client.post("/profile", data={"preferred_titles": "Java Backend Engineer, SDE I",
                                      "job_search_priorities": "Learning, location"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert ps.get_profile(db).preferred_titles == ["Java Backend Engineer", "SDE I"]
    cert = db.scalar(select(Certification))
    assert client.get(f"/profile/certifications/{cert.id}/edit").status_code == 200


def test_existing_projects_become_personal_after_upgrade(tmp_path):
    from alembic import command

    from jobpilot.config import Settings
    from jobpilot.db import alembic_config, head_revision, make_engine, migrate

    settings = Settings(_env_file=None, data_dir=tmp_path / "d")
    settings.ensure_dirs()
    eng = make_engine(settings)
    cfg = alembic_config(settings)
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0006")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO projects (name, status, created_at, updated_at) VALUES "
                          "('Old project', 'confirmed', '2026-01-01', '2026-01-01')"))
    res = migrate(settings, eng)
    assert res["from"] == "0006" and res["to"] == head_revision(settings) and res["backup"]
    with eng.connect() as conn:
        assert conn.execute(text("select name, kind from projects")).fetchall() == [("Old project", "personal")]
        assert conn.execute(text("select count(*) from certifications")).scalar() == 0
    eng.dispose()
