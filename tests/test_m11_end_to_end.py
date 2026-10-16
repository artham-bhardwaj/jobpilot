"""Milestone 11 end-to-end: the principal workflow driven mostly through the web UI.

profile (testing career, personal project) → resume import & approval → job import from a recorded Greenhouse
fixture → re-run (dedup, stable) → match explanation → skill gaps → application preparation → approval required →
simulated submission with evidence (no employer contacted) → scheduled reminders (no duplicates) → follow-up →
dashboard shows the right next action.

Offline: the job board is a fake HTTP session; no other network access is possible (DNS is faked).
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from jobpilot.models import (Application, ApplicationEvent, AuditLog, EmailDraft, Job, ReviewItem, ResumeVersion,
                             Resume, TaskRun, utcnow)
from jobpilot.services import jobs as job_svc
from jobpilot.services import scheduler, settings_store
from tests.helpers import fixture_text, make_http

GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def _ok(r, contains=None):
    assert r.status_code in (200, 303), r.status_code
    if r.status_code == 303:
        assert "err=" not in r.headers["location"], r.headers["location"]
    if contains:
        assert contains in r.text, contains
    return r


def test_principal_workflow_end_to_end(app, client, db, settings):
    # 1. Candidate profile, entered and confirmed through the UI.
    _ok(client.post("/profile", data={"full_name": "Test Candidate", "email": "test.candidate@example.com",
                                      "current_location": "Bengaluru", "preferred_titles": "Backend Engineer, SDE I"}))
    _ok(client.post("/profile/confirm"))
    for skill in ("Java", "SQL", "REST API"):
        _ok(client.post("/profile/skills", data={"name": skill, "status": "confirmed",
                                                 "evidence": "synthetic evidence"}))
    _ok(client.post("/profile/skills", data={"name": "Spring Boot", "status": "learning"}))
    _ok(client.post("/profile/employment", data={
        "company": "Synthetic Consultancy Services", "title": "Systems Engineer", "employment_type": "full_time",
        "role_type": "testing_qa", "start_date": "2025-01-01", "description": "Integration testing of Java and SQL systems",
        "status": "confirmed"}))
    _ok(client.post("/profile/projects", data={"name": "Synthetic Ledger", "kind": "personal",
                                               "technologies": "Java, SQL", "status": "confirmed"}))

    # 2. Resume: slot, import (stored unchanged as a draft), review, approval.
    _ok(client.post("/resumes/slots"))
    slot = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    text = b"Test Candidate\nSystems Engineer, Synthetic Consultancy Services\n- Integration testing\nSKILLS\nJava, SQL, REST API\n"
    _ok(client.post(f"/resumes/{slot.id}/upload", files={"file": ("resume.txt", text, "text/plain")}))
    version = db.scalar(select(ResumeVersion).where(ResumeVersion.resume_id == slot.id))
    assert version.status == "draft" and (settings.files_dir / version.file_path).read_bytes() == text
    _ok(client.get(f"/resumes/{slot.id}"), "First version of this resume")
    _ok(client.post(f"/resumes/versions/{version.id}/approve"))
    db.expire_all()

    # 3. Job import from a recorded fixture through the Greenhouse adapter; a second run changes nothing.
    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                             config={"board_token": "examplecorp"}))
    http = make_http(settings, {GH: (200, fixture_text("greenhouse_jobs.json"))})
    job_svc.collect_all(db, settings, http)
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, fixture_text("greenhouse_jobs.json"))}))
    db.commit()
    assert db.scalar(select(func.count(Job.id))) == 2, "re-running a source never duplicates jobs"
    assert set(http.session.calls) <= {GH, GH + "?content=true"}, http.session.calls
    job = db.scalar(select(Job).where(Job.external_id == "1001"))

    # 4. Match explanation: required vs preferred skills, and testing years not counted as development years.
    page = _ok(client.get(f"/jobs/{job.id}")).text
    assert "kafka" in job.match_details["skills"]["preferred"] and "java" in job.match_details["skills"]["mandatory"]
    exp = next(c for c in job.match_details["eligibility"]["checks"] if c["name"] == "experience")
    assert exp["status"] == "warn" and "0 in software development roles" in exp["explanation"]
    assert "software development roles" in page

    # 5. Skill gaps: Spring Boot is being learned, not confirmed; Kafka is a preferred gap.
    job.user_status = "shortlisted"
    db.commit()
    gaps = _ok(client.get("/skills?scope=shortlisted"), "Learning checklist").text
    assert "spring boot" in gaps and "kafka" in gaps

    # 6. Application preparation, with next action and resume selection.
    r = _ok(client.post(f"/jobs/{job.id}/application"))
    application = db.scalar(select(Application).where(Application.job_id == job.id))
    assert r.headers["location"].startswith(f"/applications/{application.id}")
    assert application.resume_version_id == version.id
    prep = application.preparation
    assert prep["resume"]["label"] == "Java Backend v1" and prep["resume"]["claims"]["contradictions"] == []
    assert {"kind": "personal project (not work experience)", "label": "Synthetic Ledger",
            "skills": ["java", "sql"]} in prep["facts"]["experience"]
    _ok(client.get(f"/applications/{application.id}"), "Next action:")

    # 7. Approval is enforced: no submission record without readiness, approval, evidence and attestation.
    r = client.post(f"/applications/{application.id}/submitted", data={"method": "confirmation_email", "attest": "1"})
    assert "err=" in r.headers["location"]
    for key in ("review_job", "apply_link", "answers"):
        _ok(client.post(f"/applications/{application.id}/check", data={"key": key, "done": "1"}))
    _ok(client.post(f"/applications/{application.id}/status", data={"to": "ready_for_review"}))
    r = client.post(f"/applications/{application.id}/submitted", data={"method": "confirmation_email", "attest": "1"})
    assert "err=" in r.headers["location"], "approval comes first"
    _ok(client.post(f"/applications/{application.id}/approve-submission"))
    r = client.post(f"/applications/{application.id}/submitted", data={"method": "confirmation_email", "attest": "1"})
    assert "err=" in r.headers["location"], "evidence comes first"

    # 8. Simulated submission (you submit on the employer's site; JobPilot only records the evidence).
    _ok(client.post(f"/applications/{application.id}/evidence",
                    data={"kind": "confirmation_email", "text": "Thanks for applying (synthetic)"}))
    _ok(client.post(f"/applications/{application.id}/submitted", data={"method": "confirmation_email", "attest": "1"}))
    db.expire_all()
    application = db.get(Application, application.id)
    assert application.status == "applied" and application.applied_at is not None

    # 9. Three weeks later: scheduled reminders raise one "check on it" item, never a rejection, never twice.
    for ev in db.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == application.id)):
        ev.ts -= timedelta(days=30)
    cfg = settings_store.get(db, "scheduler")
    cfg["tasks"]["reminders"] = {"enabled": True, "every_hours": 12}
    settings_store.put(db, "scheduler", cfg)
    db.commit()
    assert scheduler.run_due(settings, app.state.sessionmaker) == {"reminders": "success"}
    _ok(client.post("/system/tasks/reminders"))  # a manual run on top of the scheduled one
    db.expire_all()
    stale = list(db.scalars(select(ReviewItem).where(ReviewItem.kind == "application_stale")))
    assert len(stale) == 1 and db.get(Application, application.id).status == "applied"

    # 10. Follow-up draft (never sent without approval; nothing is sent here at all).
    _ok(client.get("/review?category=application_check"), "Applications to check on")
    from jobpilot.services import drafts as ds

    draft = ds.create_draft(db, ds.DraftCreate(template_key="follow_up", application_id=application.id))
    db.commit()
    assert draft.status == "draft" and draft.sent_at is None

    # 11. Dashboard: the next important action and the counts reflect this state.
    dash = _ok(client.get("/")).text
    assert "Next:" in dash and "awaiting your approval" in dash and "last discovery" in dash
    assert db.scalar(select(func.count(EmailDraft.id)).where(EmailDraft.status == "sent")) == 0
    assert not db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action.like("gmail.%")))
    assert db.scalar(select(func.count(TaskRun.id)).where(TaskRun.status == "failed")) == 0
    assert utcnow() - application.applied_at < timedelta(minutes=5)
