"""Milestone 11: resume version comparison, claim checks and the approval acknowledgement gate.

Synthetic data only.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from jobpilot.models import AuditLog
from jobpilot.services import profile as ps
from jobpilot.services import resumes as rs
from tests.helpers import seed_profile

BASE = """Test Candidate
EXPERIENCE
Systems Engineer, Synthetic Consultancy Services
- Integration testing of payment APIs
- Wrote test plans and defect reports
SKILLS
Java, SQL
"""


def _resume(db):
    return rs.create_resume(db, "Java Backend", "java_backend")


def _import(db, settings, r, text, name):
    return rs.import_version(db, settings, r, name, text.encode())


def _testing_role(db):
    for e in ps.list_facts(db, "employment"):
        ps.delete_fact(db, "employment", e.id)
    ps.save_fact(db, "employment", ps.EmploymentIn(
        company="Synthetic Consultancy Services", title="Systems Engineer", employment_type="full_time",
        role_type="testing_qa", start_date=date(2025, 1, 1), status="confirmed"))
    db.flush()


def test_first_version_has_no_comparison_and_flags_unconfirmed_skills(db, settings):
    seed_profile(db, skills=("java",), learning=())
    v = _import(db, settings, _resume(db), BASE, "v1.txt")
    review = rs.review_version(db, v)
    assert review["comparison"]["first_version"] and review["comparison"]["previous"] is None
    assert "sql" in review["claims"]["unconfirmed_skills"] and "java" not in review["claims"]["unconfirmed_skills"]
    assert not review["needs_acknowledgement"], "a first version is reviewed by the normal approval step"
    rs.approve_version(db, v)
    assert v.status == "approved" and v.validation["approval_review"]["unconfirmed_skills"] == ["sql"]


def test_material_change_needs_acknowledgement_and_is_recorded(db, settings):
    seed_profile(db, skills=("java", "sql"), learning=())
    r = _resume(db)
    rs.approve_version(db, _import(db, settings, r, BASE, "v1.txt"))
    v2 = _import(db, settings, r, BASE.replace("Java, SQL", "Java, SQL, Kafka, Docker")
                 + "- Reduced test cycle time by 40%\n", "v2.txt")
    review = rs.review_version(db, v2)
    c = review["comparison"]
    assert c["previous"] == "Java Backend v1" and c["material"]
    assert c["skills_added"] == ["docker", "kafka"] and c["skills_removed"] == []
    assert any("40%" in m for m in c["new_metric_lines"])
    assert any(d.startswith("+") and "Kafka" in d for d in c["diff"])
    with pytest.raises(rs.ResumeError, match="material changes since Java Backend v1"):
        rs.approve_version(db, v2)
    assert v2.status == "draft"
    rs.approve_version(db, v2, acknowledged=True)
    assert v2.status == "approved" and v2.validation["approval_review"]["acknowledged"] is True
    entry = db.scalars(select(AuditLog).where(AuditLog.action == "resume.version.approved")
                       .order_by(AuditLog.id.desc())).first()
    assert entry.details["acknowledged"] is True and entry.details["material"] is True


def test_cosmetic_change_does_not_need_acknowledgement(db, settings):
    seed_profile(db, skills=("java", "sql"), learning=())
    r = _resume(db)
    rs.approve_version(db, _import(db, settings, r, BASE, "v1.txt"))
    v2 = _import(db, settings, r, BASE.replace("Wrote test plans", "Wrote the test plans"), "v2.txt")
    review = rs.review_version(db, v2)
    assert not review["comparison"]["material"] and not review["needs_acknowledgement"]
    rs.approve_version(db, v2)


def test_development_wording_under_a_testing_role_is_flagged(db, settings):
    seed_profile(db, skills=("java", "sql"), learning=())
    _testing_role(db)
    text = BASE.replace("- Integration testing of payment APIs", "- Developed backend microservices in Java")
    v = _import(db, settings, _resume(db), text, "v1.txt")
    review = rs.review_version(db, v)
    assert review["needs_acknowledgement"]
    (flag,) = review["claims"]["contradictions"]
    assert "Developed backend microservices" in flag and "testing qa" in flag
    with pytest.raises(rs.ResumeError, match="contradict your recorded kind of work"):
        rs.approve_version(db, v)


def test_initials_of_employer_are_recognised_and_other_sections_are_not(db, settings):
    seed_profile(db, skills=("java",), learning=())
    _testing_role(db)
    text = ("Systems Engineer, SCS\n- Integration testing\nPROJECTS\n- Built a URL shortener in Java\n")
    review = rs.review_version(db, _import(db, settings, _resume(db), text, "v1.txt"))
    assert review["claims"]["contradictions"] == [], "a personal project after the PROJECTS heading is not work"
    text2 = "Systems Engineer, SCS\n- Implemented REST services\n"
    review2 = rs.review_version(db, _import(db, settings, _resume(db), text2, "v2.txt"))
    assert len(review2["claims"]["contradictions"]) == 1


def test_no_contradiction_when_role_is_development_or_unrecorded(db, settings):
    seed_profile(db, skills=("java",), learning=())
    text = "Systems Engineer, Synthetic Consultancy Services\n- Developed services\n"
    assert rs.claim_check(db, text)["contradictions"] == []  # seed role has no recorded kind of work


def test_web_shows_review_and_requires_checkbox(client, db, settings):
    seed_profile(db, skills=("java", "sql"), learning=())
    r = _resume(db)
    rs.approve_version(db, _import(db, settings, r, BASE, "v1.txt"))
    v2 = _import(db, settings, r, BASE.replace("Java, SQL", "Java, SQL, Kafka"), "v2.txt")
    db.commit()
    page = client.get(f"/resumes/{r.id}").text
    assert "material change" in page and "I reviewed these changes" in page and "kafka" in page
    resp = client.post(f"/resumes/versions/{v2.id}/approve")
    assert resp.status_code == 303 and "err=" in resp.headers["location"]
    db.expire_all()
    assert db.get(type(v2), v2.id).status == "draft"
    resp = client.post(f"/resumes/versions/{v2.id}/approve", data={"acknowledge": "1"})
    assert "msg=" in resp.headers["location"]
    db.expire_all()
    assert db.get(type(v2), v2.id).status == "approved"
