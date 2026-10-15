"""Milestone 8 acceptance: one end-to-end workflow mapped to the 18 acceptance criteria.

Offline: the Greenhouse board is a recorded synthetic fixture served by a fake HTTP session; DNS is faked;
the browser step uses headless Chrome against a synthetic page on 127.0.0.1. No paid service or LLM.
"""

from __future__ import annotations

import importlib.util
import sqlite3

import pytest
from sqlalchemy import select

from jobpilot.models import Application, AuditLog, EmailDraft, Job, ReviewItem
from jobpilot.services import applications as apps
from jobpilot.services import backup, queue
from jobpilot.services import jobs as job_svc
from tests.helpers import LocalPage, fixture_text, make_http, make_pdf_bytes

GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def test_milestone8_end_to_end(client, db, settings, app, monkeypatch):
    loc = lambda r: r.headers["location"]  # noqa: E731

    # Profile (synthetic) via the UI.
    client.post("/profile", data={"full_name": "Test Candidate", "first_name": "Test", "last_name": "Candidate",
                                  "email": "test.candidate@example.com", "total_experience_years": "1.5",
                                  "current_location": "Bengaluru", "acceptable_locations": "Bengaluru",
                                  "needs_sponsorship": "false", "willing_to_relocate": "false"})
    client.post("/profile/confirm")
    for sk in ("Java", "Spring Boot", "SQL", "REST API"):
        client.post("/profile/skills", data={"name": sk, "status": "confirmed"})
    client.post("/profile/skills", data={"name": "Docker", "status": "unverified"})

    # 1. Import from a supported source adapter (fixture), via the web "run source" path.
    client.post("/sources", data={"name": "Examplecorp", "kind": "greenhouse", "enabled": "1", "company_name": "Examplecorp",
                                  "config": '{"board_token": "examplecorp"}'})
    monkeypatch.setattr(job_svc, "PoliteHttp", lambda s: make_http(s, {GH: (200, fixture_text("greenhouse_jobs.json"))}))
    client.post("/sources/run", data={})
    db.expire_all()
    job = db.scalar(select(Job).where(Job.external_id == "1001"))
    assert job is not None and len(list(db.scalars(select(Job)))) == 2

    # 2. Duplicates: a second run and a manual copy of the same posting don't create new jobs.
    client.post("/sources/run", data={})
    client.post("/jobs/manual", data={"title": "Software Engineer I, Backend (Java)", "company_name": "Examplecorp",
                                      "apply_url": "https://boards.greenhouse.io/examplecorp/jobs/1001?gh_src=feed"})
    db.expire_all()
    assert len(list(db.scalars(select(Job)))) == 2
    job = db.get(Job, job.id)
    assert len(job.sightings) == 2

    # 3. Details and provenance preserved.
    assert "<h3>Requirements</h3>" in job.description_raw and job.posted_at and job.first_seen_at
    assert job.sightings[0].method == "api" and job.title == "Software Engineer I, Backend (Java)"

    # 4. Required vs preferred skills.
    sk = job.match_details["skills"]
    assert {"java", "spring boot", "sql"} <= set(sk["mandatory"]) and {"kafka", "docker"} <= set(sk["preferred"])

    # 5. Relevance and eligibility are separate; 6. explanations carry evidence.
    rel, elig = job.match_details["relevance"], job.match_details["eligibility"]
    assert rel["score"] == job.relevance_score and elig["status"] == job.eligibility
    req = next(c for c in rel["components"] if c["key"] == "required_skills")
    assert any("requirements section" in e for e in req["evidence"])
    assert "docker" in sk["unverified"]
    senior = db.scalar(select(Job).where(Job.external_id == "1002"))
    assert senior.hard_conflict and senior.match_details["recommendation"]["key"] == "not_suitable"

    # 7. Missing-resume warning, then correct approved resume selected.
    assert "No approved resume" in client.get(f"/jobs/{job.id}/resume-review").text
    client.post("/resumes/slots")
    from jobpilot.models import Resume

    jr = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    client.post(f"/resumes/{jr.id}/upload", files={"file": ("java.pdf", make_pdf_bytes(["Test Candidate", "Java Spring Boot SQL REST APIs"]),
                                                            "application/pdf")})
    db.expire_all()
    version = db.get(Resume, jr.id).versions[0]
    client.post(f"/resumes/versions/{version.id}/approve")
    assert "Java Backend v1" in client.get(f"/jobs/{job.id}/resume-review").text

    # 11. The opportunity is in the review queue (before an application exists).
    assert any(i["job_id"] == job.id for i in queue.build(db))

    # 8. Preparation record persisted.
    client.post(f"/jobs/{job.id}/application", data={})
    db.expire_all()
    application = db.scalar(select(Application).where(Application.job_id == job.id))
    assert application.preparation and application.preparation["resume"]["label"] == "Java Backend v1"
    assert application.resume_version_id == version.id

    # 9. Recruiter draft from verified facts (the address published in the posting).
    client.post(f"/jobs/{job.id}/contacts-from-posting")
    db.expire_all()
    from jobpilot.models import Contact

    contact = db.scalar(select(Contact))
    assert contact.source_type == "job_posting" and contact.email_status == "unverified"
    client.post("/drafts", data={"template_key": "recruiter_outreach", "job_id": job.id, "contact_id": contact.id,
                                 "application_id": application.id})
    draft = db.scalar(select(EmailDraft))
    assert "Test Candidate" in draft.body and draft.resume_version_id == version.id

    # 10. Missing information blocks unsafe approval (drafts and applications).
    client.post(f"/drafts/{draft.id}", data={"to_address": draft.to_address, "subject": draft.subject,
                                             "body": draft.body + "\n[[MISSING: referral name]]"})
    assert "err=" in loc(client.post(f"/drafts/{draft.id}/approve"))
    client.post(f"/applications/{application.id}/questions", data={"questions": "Are you legally authorized to work in India?"})
    for key in ("review_job", "apply_link", "answers"):
        client.post(f"/applications/{application.id}/check", data={"key": key, "done": "1"})
    assert "question" in loc(client.post(f"/applications/{application.id}/status", data={"to": "ready_for_review"}))
    db.expire_all()
    assert db.get(Application, application.id).status == "preparing"

    # 13. Browser assistance stops before final submission (synthetic page on 127.0.0.1).
    if importlib.util.find_spec("playwright"):
        from jobpilot.automation import apply as apply_mod

        page = LocalPage('<form><label for=fn>First Name *</label><input id=fn name=first_name required>'
                         '<button type=submit>Submit application</button></form>')
        try:
            settings.browser_enabled, settings.browser_headless = True, True
            monkeypatch.setattr(apply_mod, "site_for", lambda url: "greenhouse")
            a = db.get(Application, application.id)
            a.apply_url = page.url
            run = apply_mod.run_assist(db, settings, a, wait_for_user=False, approved_host=page.host)
            if not (run.status == "failed" and "Executable" in (run.message or "")):
                assert run.status in ("filled", "paused") and a.applied_at is None and a.status == "preparing"
        finally:
            page.close()
            db.commit()

    # 12. Transitions validated and audited; submission only with evidence.
    q = db.scalar(select(__import__("jobpilot.models", fromlist=["ApplicationQuestion"]).ApplicationQuestion))
    client.post(f"/applications/questions/{q.id}", data={"answer": "Yes"})
    client.post(f"/applications/questions/{q.id}", data={"action": "approve", "answered_myself": "1"})
    assert "msg=" in loc(client.post(f"/applications/{application.id}/status", data={"to": "ready_for_review"}))
    assert "err=" in loc(client.post(f"/applications/{application.id}/status", data={"to": "offer"}))
    client.post(f"/applications/{application.id}/approve-submission")
    assert "err=" in loc(client.post(f"/applications/{application.id}/submitted",
                                     data={"method": "confirmation_email", "attest": "1"}))
    client.post(f"/applications/{application.id}/evidence", data={"kind": "confirmation_email",
                                                                  "text": "Thank you for applying (synthetic)"})
    client.post(f"/applications/{application.id}/submitted", data={"method": "confirmation_email", "attest": "1"})
    db.expire_all()
    a = db.get(Application, application.id)
    assert a.status == "applied" and a.applied_at
    assert [e.to_status for e in a.events][-2:] == ["ready_for_review", "applied"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "application.submitted"))

    # 14. Security controls still effective.
    assert client.post("/system/backup", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/api/docs").status_code == 404
    assert client.get("/").headers["referrer-policy"] == "same-origin"

    # 15. Backup/restore without data loss.
    job_id, app_id = job.id, application.id
    path = backup.create_backup(settings, app.state.engine, label="m8-accept")
    db.close()
    backup.restore_backup(settings, path, app.state.engine)
    con = sqlite3.connect(settings.db_file)
    assert con.execute("select status from applications").fetchone()[0] == "applied"
    assert con.execute("select count(*) from jobs").fetchone()[0] == 2
    con.close()

    # 16. Dashboard and core pages work.
    for url in ("/", "/jobs", f"/jobs/{job_id}", "/sources", "/matching", "/review", "/applications",
                f"/applications/{app_id}", "/contacts", "/companies", "/drafts", "/answers", "/analytics", "/system"):
        assert client.get(url).status_code == 200, url


def test_no_paid_ai_or_llm_dependencies():
    """Criterion 18: no AI SDKs among dependencies or imports."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    deps = (root / "pyproject.toml").read_text().lower()
    code = " ".join(p.read_text().lower() for p in (root / "src").rglob("*.py"))
    for name in ("openai", "anthropic", "langchain", "transformers", "llama", "cohere", "google.generativeai", "mistral"):
        assert name not in deps, name
        assert f"import {name}" not in code and f"from {name}" not in code, name
