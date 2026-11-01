"""Job desk: generate a resume for a job, apply, record and delete, all from one page; deleted jobs stay deleted."""

from __future__ import annotations

from sqlalchemy import select

from jobpilot.models import Application, DeletedJob, Job, ReviewItem, TailoredResume
from jobpilot.services import job_delete, tailored
from jobpilot.services import jobs as job_svc
from jobpilot.sources.base import JobData
from tests.test_m34_tailored import needs_tex, tworld  # noqa: F401  (fixture)


def _posting(title="Backend Engineer A", url="https://boards.example.com/jobs/a", ext="a-1"):
    return JobData(title=title, company_name="Examplecorp", location="Pune, India", apply_url=url,
                   description_text="Java, SQL (synthetic).", external_id=ext)


def test_desk_lists_focus_jobs_with_actions(client, db):
    job, _ = job_svc.upsert(db, None, _posting(), "manual")
    job.category, job.relevance_score = "java_backend", 80.0
    senior, _ = job_svc.upsert(db, None, _posting("Senior Engineer", "https://boards.example.com/jobs/s", "s-1"), "manual")
    senior.category = "java_backend"
    db.commit()
    page = client.get("/desk").text
    assert "Job desk" in page and "Backend Engineer A" in page and "Senior Engineer" not in page
    assert f'action="/desk/{job.id}/delete"' in page and f'action="/tracker/jobs/{job.id}/applied"' in page
    assert "Apply on company site" in page and 'href="/desk"' in client.get("/").text, "sidebar tab"
    assert "Senior Engineer" in client.get("/desk?scope=any").text
    assert "Backend Engineer A" not in client.get("/desk?q=python").text
    assert 'To apply <span class="n">1</span>' in page


@needs_tex
def test_generate_resume_and_record_from_the_desk(tworld, client, db):  # noqa: F811
    j = tworld.job
    page = client.get("/desk?scope=any").text
    assert "Generate resume for this job" in page
    r = client.post(f"/jobs/{j.id}/tailored-resume", data={"next": "/desk?scope=any#job-%d" % j.id},
                    follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("/desk?scope=any") and "ready" in loc and loc.endswith(f"#job-{j.id}")
    db.expire_all()
    tr = tailored.latest(db, j.id, "valid")
    page = client.get("/desk?scope=any").text
    assert "Tailored resume ready" in page and f"/tailored-resumes/{tr.id}/download" in page and "Regenerate" in page
    assert '<option value="tailored" selected>' in page

    r = client.post(f"/tracker/jobs/{j.id}/applied", data={"resume_version_id": "tailored", "next": "/desk?scope=any"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/desk?scope=any")
    applied = client.get("/desk?scope=any&tab=applied").text
    assert "✓ Applied" in applied and "has application" in applied
    assert client.post(f"/desk/{j.id}/delete").status_code == 303
    db.expire_all()
    assert db.get(Job, j.id) is not None, "a job with an application is kept"


def test_delete_removes_the_job_and_keeps_it_out(client, db, settings):
    job, _ = job_svc.upsert(db, None, _posting(), "manual")
    pdf = settings.files_dir / "tailored_resumes" / "x.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    db.add(TailoredResume(job_id=job.id, company_name="Examplecorp", job_title=job.title, status="valid",
                          inputs_sha256="-", pdf_path="tailored_resumes/x.pdf"))
    db.add(ReviewItem(kind="job_review", entity_type="job", entity_id=job.id, title="t", status="open"))
    db.commit()
    jid = job.id

    r = client.post(f"/desk/{jid}/delete", data={"next": "/desk?tab=all"}, follow_redirects=False)
    assert r.headers["location"].startswith("/desk?tab=all") and "Deleted" in r.headers["location"]
    db.expire_all()
    assert db.get(Job, jid) is None and not pdf.exists()
    assert db.scalar(select(TailoredResume)) is None and db.scalar(select(ReviewItem)) is None
    assert db.scalar(select(DeletedJob)).title == "Backend Engineer A"

    # A source lists it again (same URL, or same company / title / location): skipped.
    stats = job_svc.ingest(db, None, [_posting(), _posting(url="https://other.example.com/x")], "import")
    assert stats["skipped_deleted"] == 2 and stats["new"] == 0
    # Adding it yourself brings it back and clears the memory.
    back, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Backend Engineer A", company_name="Examplecorp",
                                                         location="Pune, India",
                                                         apply_url="https://boards.example.com/jobs/a"))
    assert back.id and db.scalar(select(DeletedJob)) is None


def test_job_with_application_cannot_be_deleted(db, settings):
    job, _ = job_svc.upsert(db, None, _posting(), "manual")
    db.add(Application(job_id=job.id, status="applied"))
    db.commit()
    assert "application" in (job_delete.blocker(db, job) or "")
    try:
        job_delete.delete(db, settings, job)
        raise AssertionError("should refuse")
    except job_delete.DeleteError:
        pass
