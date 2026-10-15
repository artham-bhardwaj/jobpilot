"""End-to-end acceptance tests mirroring the project's acceptance criteria.

Everything runs offline against synthetic fixtures; no paid services or AI APIs.
"""

from __future__ import annotations

import csv
import io
import socket
import subprocess
import sys
import time

import requests
from sqlalchemy import select

from jobpilot.models import Application, EmailDraft, Job, Resume, ResumeVersion
from tests.helpers import fixture_text, make_pdf_bytes


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_01_starts_locally_and_serves_dashboard(tmp_path):
    port = _free_port()
    env = {"JOBPILOT_DATA_DIR": str(tmp_path / "d"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    # README order: init, then serve (serve refuses to create a database implicitly).
    init = subprocess.run([sys.executable, "-m", "jobpilot.cli", "init"], env=env, cwd=tmp_path, capture_output=True,
                          text=True, timeout=120)
    assert init.returncode == 0, init.stderr
    proc = subprocess.Popen([sys.executable, "-m", "jobpilot.cli", "serve", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=tmp_path)
    try:
        for _ in range(60):
            try:
                r = requests.get(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.25)
        else:
            raise AssertionError("server did not start")
        assert r.status_code == 200 and "Overview" in r.text
        assert requests.get(f"http://127.0.0.1:{port}/health", timeout=2).json()["ok"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_02_to_13_full_workflow(client, db, settings, app):
    loc = lambda r: r.headers["location"]  # noqa: E731

    # 2. Save and retrieve candidate profile data
    client.post("/profile", data={"full_name": "Test Candidate", "first_name": "Test", "last_name": "Candidate",
                                  "email": "test.candidate@example.com", "total_experience_years": "1.5",
                                  "current_location": "Bengaluru", "acceptable_locations": "Bengaluru, Pune",
                                  "needs_sponsorship": "false"})
    client.post("/profile/confirm")
    for skill in ("Java", "Spring Boot", "SQL", "REST API"):
        client.post("/profile/skills", data={"name": skill, "status": "confirmed"})
    page = client.get("/profile").text
    assert "Test Candidate" in page and "Spring Boot" in page

    # 3 + 4. Import a job from a supported source; parse and persist details
    r = client.post("/jobs/import", files={"file": ("jobs.csv", fixture_text("jobs_import.csv").encode(), "text/csv")})
    assert "msg=" in loc(r)
    job = db.scalar(select(Job).where(Job.title == "Java Developer"))
    assert job.company_name == "Demo Widgets" and job.experience_min_years == 1 and job.remote_type == "onsite"
    assert job.posted_at is not None and job.sightings

    # 5. Calculate and explain a job-match score
    assert job.relevance_score is not None and job.match_details["relevance"]["components"]
    assert all(c["explanation"] for c in job.match_details["relevance"]["components"])
    assert job.eligibility in ("eligible", "likely_eligible", "unknown", "ineligible")

    # 6. Select an appropriate approved resume
    client.post("/resumes/slots")
    java = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    client.post(f"/resumes/{java.id}/upload", files={"file": ("java.pdf", make_pdf_bytes(["Test Candidate", "Java Spring Boot SQL"]), "application/pdf")})
    v = db.scalar(select(ResumeVersion))
    client.post(f"/resumes/versions/{v.id}/approve")
    assert "Java Backend v1" in client.get(f"/jobs/{job.id}").text

    # 7. Create an application preparation record
    r = client.post(f"/jobs/{job.id}/application", data={})
    app_rec = db.scalar(select(Application))
    assert app_rec.status == "preparing" and app_rec.resume_version_id == v.id

    # 8. Editable recruiter email draft when suitable contact data exists
    r = client.post("/contacts", data={"name": "Jane Recruiter", "company_name": "Demo Widgets",
                                       "email": "jane.recruiter@example.com", "source_type": "company_website",
                                       "source_url": "https://careers.example.org/team"})
    contact_id = int(loc(r).split("/contacts/")[1].split("?")[0])
    r = client.post("/drafts", data={"template_key": "recruiter_outreach", "job_id": job.id, "contact_id": contact_id})
    draft = db.scalar(select(EmailDraft))
    assert "Hi Jane" in draft.body and not draft.missing_fields
    client.post(f"/drafts/{draft.id}", data={"to_address": draft.to_address, "subject": "Edited subject", "body": draft.body})
    db.refresh(draft)
    assert draft.subject == "Edited subject"

    # 9 + 10. Track through lifecycle; submission only after actual confirmation
    for key in ("review_job", "apply_link", "answers"):
        client.post(f"/applications/{app_rec.id}/check", data={"key": key, "done": "1"})
    client.post(f"/applications/{app_rec.id}/status", data={"to": "ready_for_review"})
    client.post(f"/applications/{app_rec.id}/approve-submission")
    r = client.post(f"/applications/{app_rec.id}/submitted", data={"method": "confirmation_email", "attest": "1"})
    assert "err=" in loc(r), "no confirmation evidence -> not submitted"
    db.refresh(app_rec)
    assert app_rec.status == "ready_for_review" and app_rec.applied_at is None
    client.post(f"/applications/{app_rec.id}/evidence",
                data={"kind": "confirmation_email", "text": "Thank you for applying to Demo Widgets (synthetic)"})
    client.post(f"/applications/{app_rec.id}/submitted", data={"method": "confirmation_email", "attest": "1"})
    for to in ("interview", "rejected"):
        client.post(f"/applications/{app_rec.id}/status", data={"to": to})
    db.refresh(app_rec)
    assert app_rec.status == "rejected" and app_rec.applied_at is not None
    assert [e.to_status for e in app_rec.events] == ["preparing", "ready_for_review", "applied", "interview", "rejected"]

    # 11. Analytics and CSV exports from stored records
    assert "Conversion" in client.get("/analytics").text
    rows = list(csv.DictReader(io.StringIO(client.get("/analytics/export/applications.csv").text)))
    assert rows[0]["status"] == "rejected" and rows[0]["resume"] == "Java Backend v1"

    # 12. Back up and restore the local database
    from jobpilot.services import backup

    assert "msg=" in loc(client.post("/system/backup"))
    name = backup.list_backups(settings)[0]["name"]
    client.post(f"/applications/{app_rec.id}/notes", data={"notes": "after backup"})
    r = client.post("/system/restore", data={"name": name, "confirm": "RESTORE"})
    assert "msg=" in loc(r)
    with app.state.sessionmaker() as s:
        restored = s.get(Application, app_rec.id)
        assert restored.status == "rejected" and restored.notes is None
