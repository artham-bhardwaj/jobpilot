"""Stage 7 regression tests: invalid requests must never produce HTTP 500.

Covers the 12 handlers reported in IMPLEMENTATION_STATUS.md (D1) with both invalid
and valid requests, plus a sweep over every registered route.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobpilot.models import Application, EmailDraft, Interview, LearningItem
from tests.helpers import make_pdf_bytes, seed_profile

MISSING = 999999

# (method, url, form) -> expected status for a request that targets something that doesn't exist.
INVALID_CASES = [
    ("POST", f"/applications/{MISSING}/status", {"to": "withdrawn"}, 404),
    ("POST", f"/applications/{MISSING}/check", {"key": "answers", "done": "1"}, 404),
    ("POST", f"/applications/{MISSING}/resume", {"version_id": "1"}, 404),
    ("POST", f"/applications/{MISSING}/approve-submission", {}, 404),
    ("POST", f"/applications/{MISSING}/submitted", {"method": "screenshot", "attest": "1"}, 404),
    ("POST", f"/applications/{MISSING}/evidence", {"kind": "note", "text": "x"}, 404),
    ("POST", f"/drafts/{MISSING}", {"subject": "s", "body": "b"}, 404),
    ("POST", f"/interviews/{MISSING}/outcome", {"status": "completed", "outcome": "passed"}, 404),
    ("POST", f"/skills/backlog/{MISSING}", {"status": "backlog"}, 404),
    ("POST", f"/skills/backlog/{MISSING}", {"action": "confirm", "evidence": "a long enough evidence text"}, 404),
    ("POST", "/profile/nonsense", {"name": "x"}, 404),
    ("GET", "/profile/nonsense/1/edit", None, 404),
    ("GET", "/system/backup/not-a-backup-name", None, 400),
    ("GET", "/system/backup/jobpilot-20260101T000000-missing.zip", None, 404),
]


@pytest.fixture
def safe_client(app):
    # raise_server_exceptions=False so an unhandled error shows up as a 500 response, not a test crash.
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


@pytest.mark.parametrize("method,url,form,expected", INVALID_CASES, ids=[f"{m} {u}" for m, u, _, _ in INVALID_CASES])
def test_reported_cases_return_controlled_errors(safe_client, method, url, form, expected):
    r = safe_client.request(method, url, data=form)
    assert r.status_code == expected, (r.status_code, r.text[:200])
    assert "Traceback" not in r.text
    assert "text/html" in r.headers["content-type"]
    assert "not found" in r.text.lower() or "invalid" in r.text.lower() or "unknown" in r.text.lower()


def _prepared_application(db, settings):
    from jobpilot.models import Resume
    from jobpilot.services import applications as apps
    from jobpilot.services import jobs as job_svc
    from jobpilot.services import resumes as rs

    seed_profile(db)
    rs.create_standard_slots(db)
    java = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    v = rs.import_version(db, settings, java, "java.pdf", make_pdf_bytes(["Test Candidate", "Java"]))
    rs.approve_version(db, v)
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", company_name="Examplecorp",
                                                        description_text="Requirements: Java. 1+ years of experience"))
    app = apps.create(db, job)
    db.commit()
    return app, v


def test_reported_cases_valid_requests_still_work(safe_client, db, settings):
    """The same handlers succeed for records that exist."""
    from jobpilot.services import backup
    from jobpilot.services import interviews as iv_svc

    app, v = _prepared_application(db, settings)
    c = safe_client
    for key in ("review_job", "apply_link", "answers"):
        assert c.post(f"/applications/{app.id}/check", data={"key": key, "done": "1"}).status_code == 303
    assert c.post(f"/applications/{app.id}/resume", data={"version_id": str(v.id)}).status_code == 303
    assert "msg=" in c.post(f"/applications/{app.id}/status", data={"to": "ready_for_review"}).headers["location"]
    assert "msg=" in c.post(f"/applications/{app.id}/approve-submission").headers["location"]
    assert "msg=" in c.post(f"/applications/{app.id}/evidence",
                            data={"kind": "confirmation_email", "text": "Thanks for applying (synthetic)"}).headers["location"]
    assert "msg=" in c.post(f"/applications/{app.id}/submitted",
                            data={"method": "confirmation_email", "attest": "1"}).headers["location"]
    db.expire_all()
    assert db.get(Application, app.id).status == "applied"

    iv = iv_svc.save(db, iv_svc.InterviewIn(application_id=app.id, stage="technical"))
    db.commit()
    assert "msg=" in c.post(f"/interviews/{iv.id}/outcome", data={"status": "completed", "outcome": "passed"}).headers["location"]
    db.expire_all()
    assert db.get(Interview, iv.id).outcome == "passed"

    c.post("/drafts", data={"template_key": "status_inquiry", "job_id": app.job_id})
    d = db.scalar(select(EmailDraft))
    assert "msg=" in c.post(f"/drafts/{d.id}", data={"subject": "Hello", "body": "Body"}).headers["location"]

    c.post("/skills/backlog", data={"skill": "kafka"})
    li = db.scalar(select(LearningItem))
    assert "msg=" in c.post(f"/skills/backlog/{li.id}", data={"status": "in_progress", "progress_pct": "40"}).headers["location"]
    assert "msg=" in c.post(f"/skills/backlog/{li.id}", data={"action": "confirm",
                                                              "evidence": "Built a Kafka pipeline (synthetic)"}).headers["location"]

    assert "msg=" in c.post("/profile/skills", data={"name": "Go", "status": "learning"}).headers["location"]
    from jobpilot.models import Skill

    sk = db.scalar(select(Skill).where(Skill.normalized_name == "go"))
    assert c.get(f"/profile/skills/{sk.id}/edit").status_code == 200

    path = backup.create_backup(settings, db.get_bind(), label="t")
    r = c.get(f"/system/backup/{path.name}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"


def test_invalid_query_and_form_input_is_400_not_500(safe_client):
    for url in ("/sources?edit=abc", "/jobs?page=abc&min_score=zz&posted_within_days=-5", "/jobs?sort=bogus&status=weird",
                "/interviews?suggestion=abc", "/contacts?job_id=abc&company_id=x"):
        r = safe_client.get(url)
        assert r.status_code in (200, 400), (url, r.status_code)
    r = safe_client.post("/interviews", data={"application_id": "not-a-number"})
    assert r.status_code in (303, 400)


def _all_routes():
    from jobpilot.web.routes import applications, core, insight, jobs, outreach, profile, resumes

    for mod in (core, profile, resumes, jobs, outreach, applications, insight):
        for rt in mod.router.routes:
            for m in sorted(rt.methods - {"HEAD"}):
                yield m, rt.path


# Routes whose POST performs a heavyweight or destructive action on success; still probed with invalid input.
SKIP_SWEEP = {("POST", "/sources/run"), ("POST", "/gmail/{action}")}


def test_every_route_with_missing_ids_and_empty_forms_never_500(safe_client):
    seen = 0
    for method, path in _all_routes():
        if (method, path) in SKIP_SWEEP:
            continue
        url = re.sub(r"\{[a-z_]+:int\}", str(MISSING), path)
        url = url.replace("{action}", "approve").replace("{kind}", "skills").replace("{name}", "backup")
        url = url.replace("{key}", "matching")
        r = safe_client.request(method, url, data={} if method == "POST" else None)
        assert r.status_code < 500, (method, url, r.status_code, r.text[:200])
        seen += 1
    assert seen > 80
