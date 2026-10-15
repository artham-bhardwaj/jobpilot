"""Stage 2 through the HTTP layer: profile, resumes, jobs, sources."""

from __future__ import annotations

from sqlalchemy import select

from jobpilot.models import CandidateProfile, Job, JobSource, Resume, ResumeVersion, Skill
from tests.helpers import fixture_text, make_pdf_bytes


def _ok(r, contains="msg="):
    assert r.status_code == 303, r.text
    assert contains in r.headers["location"], r.headers["location"]
    return r.headers["location"]


def test_profile_flow(client, db):
    assert client.get("/profile").status_code == 200
    _ok(client.post("/profile", data={"full_name": "Test Candidate", "email": "test.candidate@example.com",
                                      "total_experience_years": "1.5", "acceptable_locations": "Pune, Remote",
                                      "remote_preferences": ["remote", "hybrid"], "needs_sponsorship": "false"}))
    _ok(client.post("/profile", data={"email": "nope"}), "err=")
    _ok(client.post("/profile/confirm"))
    db.expire_all()
    p = db.scalar(select(CandidateProfile))
    assert p.full_name == "Test Candidate" and p.confirmed_at and p.remote_preferences == ["remote", "hybrid"]
    assert p.needs_sponsorship is False

    _ok(client.post("/profile/skills", data={"name": "Java", "status": "confirmed"}))
    _ok(client.post("/profile/skills", data={"name": "java"}), "err=")
    skill = db.scalar(select(Skill))
    _ok(client.post(f"/profile/skills/{skill.id}/status", data={"status": "learning"}))
    assert client.get(f"/profile/skills/{skill.id}/edit").status_code == 200
    _ok(client.post("/profile/employment", data={"company": "Synthetic Corp", "title": "SWE", "start_date": "2024-01-01",
                                                  "status": "confirmed"}))
    _ok(client.post("/profile/links", data={"label": "GitHub", "url": "https://github.com/example"}))
    page = client.get("/profile").text
    assert "Synthetic Corp" in page and "github.com/example" in page


def test_resume_flow(client, db):
    _ok(client.post("/resumes/slots"))
    assert db.scalar(select(Resume).where(Resume.category == "java_backend"))
    java = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    pdf = make_pdf_bytes(["Test Candidate", "Java Spring Boot"])
    _ok(client.post(f"/resumes/{java.id}/upload", files={"file": ("java.pdf", pdf, "application/pdf")},
                    data={"suggest_skills": "1"}))
    _ok(client.post(f"/resumes/{java.id}/upload", files={"file": ("evil.exe", b"MZ...", "application/octet-stream")}),
        "err=")
    _ok(client.post(f"/resumes/{java.id}/upload", files={"file": ("fake.pdf", b"hello", "application/pdf")}), "err=")
    v = db.scalar(select(ResumeVersion))
    assert v.status == "draft"
    assert {s.status for s in db.scalars(select(Skill))} == {"unverified"}
    _ok(client.post(f"/resumes/versions/{v.id}/approve"))
    r = client.get(f"/resumes/versions/{v.id}/download")
    assert r.status_code == 200 and r.content == pdf
    assert "attachment" in r.headers["content-disposition"]
    assert client.get(f"/resumes/{java.id}").status_code == 200
    assert client.get("/resumes").status_code == 200


def test_jobs_manual_import_and_explorer(client, db):
    _ok(client.post("/jobs/manual", data={"title": "Java Backend Engineer", "company_name": "Examplecorp",
                                          "location": "Pune", "description_text": "Requirements: Java, SQL. 1+ years of experience."}))
    _ok(client.post("/jobs/manual", data={"title": ""}), "err=")
    r = client.post("/jobs/import", files={"file": ("jobs.csv", fixture_text("jobs_import.csv").encode(), "text/csv")})
    loc = _ok(r)
    assert "2+new" in loc or "2%20new" in loc or "2 new" in loc.replace("+", " ")
    job = db.scalar(select(Job).where(Job.title == "Java Backend Engineer"))
    page = client.get(f"/jobs/{job.id}").text
    assert "Relevance" in page and "Eligibility" in page and "No approved resume" in page
    listing = client.get("/jobs?q=java&sort=newest").text
    assert "Java Backend Engineer" in listing and "Java Developer" in listing
    assert "SDE 1" not in client.get("/jobs?q=hibernate").text
    assert "SDE 1" in client.get("/jobs?remote=remote").text
    _ok(client.post(f"/jobs/{job.id}/status", data={"status": "dismissed"}))
    assert "<b>Java Backend Engineer</b>" not in client.get("/jobs").text
    assert "<b>Java Backend Engineer</b>" in client.get("/jobs?user_status=dismissed").text
    _ok(client.post("/jobs/rematch"))
    assert client.get("/jobs/new").status_code == 200


def test_sources_crud(client, db):
    _ok(client.post("/sources", data={"name": "Examplecorp", "kind": "greenhouse", "enabled": "1",
                                      "config": '{"board_token": "examplecorp"}'}))
    _ok(client.post("/sources", data={"name": "Bad", "kind": "greenhouse", "config": "not json"}), "err=")
    _ok(client.post("/sources", data={"name": "Bad", "kind": "lever", "config": '{"site": "a b"}'}), "err=")
    src = db.scalar(select(JobSource))
    assert src.config == {"board_token": "examplecorp"}
    assert client.get("/sources").status_code == 200
    assert client.get(f"/sources?edit={src.id}").status_code == 200
    _ok(client.post(f"/sources/{src.id}/delete"))
