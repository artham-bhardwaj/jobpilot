"""Stage 7 reliability tests: read-only GETs, CLI behaviour, path predictability."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from jobpilot.models import EmailDraft, utcnow
from tests.helpers import FIXTURES, make_pdf_bytes, seed_profile

REPO = Path(__file__).resolve().parents[1]


def _db_fingerprint(db_file: Path) -> str:
    con = sqlite3.connect(db_file)
    try:
        return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
    finally:
        con.close()


def _seed_everything(db, settings):
    from jobpilot.models import Resume
    from jobpilot.services import applications as apps
    from jobpilot.services import contacts as cs
    from jobpilot.services import drafts as ds
    from jobpilot.services import interviews as iv_svc
    from jobpilot.services import jobs as job_svc
    from jobpilot.services import resumes as rs

    seed_profile(db)
    rs.create_standard_slots(db)
    java = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    rs.approve_version(db, rs.import_version(db, settings, java, "j.pdf", make_pdf_bytes(["Test Candidate", "Java"])))
    job_svc.import_file(db, "jobs.csv", (FIXTURES / "jobs_import.csv").read_bytes())
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", company_name="Examplecorp",
                                                        description_text="Requirements: Java, SQL. 1+ years. jane@example.com"))
    app = apps.create(db, job)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id))
    ds.approve(db, d)
    ds.mark_sent(db, d)
    d.follow_up_due_at = utcnow() - timedelta(days=1)  # a due follow-up exists
    for key in ("review_job", "apply_link", "answers"):
        apps.set_check(db, app, key, True)
    apps.transition(db, app, "ready_for_review")
    apps.approve_submission(db, app)
    apps.add_evidence(db, settings, app, "confirmation_email", "Thanks (synthetic)")
    db.refresh(app)
    apps.record_submission(db, app, "confirmation_email", "", attest=True)
    iv = iv_svc.save(db, iv_svc.InterviewIn(application_id=app.id, stage="technical"))
    iv_svc.generate_prep_pack(db, iv)
    # States that previously triggered writes on GET:
    stale = ds.create_draft(db, ds.DraftCreate(template_key="status_inquiry", job_id=job.id))
    stale.to_address, stale.status, stale.sent_at = "x@example.org", "sent", utcnow() - timedelta(days=9)
    stale.follow_up_due_at, stale.reply_received_at = utcnow() - timedelta(days=1), utcnow()   # due + replied
    job2, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Unscored Role", company_name="Examplecorp"))
    job2.matched_at = None                                                                     # not yet scored
    app2 = apps.create(db, job2)
    app2.checklist = {}                                                                        # stale checklist
    db.commit()
    return {"job": job.id, "app": app.id, "contact": c.id, "draft": d.id, "interview": iv.id, "resume": java.id,
            "job2": job2.id, "app2": app2.id}


def test_get_requests_do_not_modify_the_database(client, db, settings):
    ids = _seed_everything(db, settings)
    db.close()
    urls = ["/", "/jobs", "/jobs?sort=newest", f"/jobs/{ids['job']}", "/jobs/new", "/review", "/applications",
            f"/applications/{ids['app']}", "/interviews", f"/interviews/{ids['interview']}", "/profile",
            "/resumes", f"/resumes/{ids['resume']}", "/skills", "/contacts", f"/contacts/{ids['contact']}", "/drafts",
            f"/drafts/{ids['draft']}", "/drafts/templates", "/analytics", "/analytics/export/jobs.csv", "/sources", "/system", "/gmail", "/health"]
    before = _db_fingerprint(settings.db_file)
    for url in urls:
        assert client.get(url).status_code == 200, url
    assert _db_fingerprint(settings.db_file) == before, "a GET request changed the database"


def test_fresh_profile_page_is_read_only(client, settings):
    before = _db_fingerprint(settings.db_file)
    for url in ("/", "/profile", "/drafts", "/drafts/templates", "/resumes"):
        assert client.get(url).status_code == 200
    assert _db_fingerprint(settings.db_file) == before


def test_due_followups_suppression_happens_in_scheduled_task_not_on_read(app, settings, db):
    from jobpilot.services import contacts as cs
    from jobpilot.services import drafts as ds
    from jobpilot.services import scheduler

    seed_profile(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", contact_id=c.id))
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject="Hello", body="Hi (synthetic)"))
    ds.approve(db, d)
    ds.mark_sent(db, d)
    d.follow_up_due_at = utcnow() - timedelta(days=1)
    cs.add_dnc(db, cs.DncIn(kind="email", value="jane@example.com"))  # add_dnc suppresses immediately
    db.commit()
    assert d.follow_up_due_at is None and d.suppressed_reason
    # A reply recorded by a stale path: due list must still exclude it without writing.
    d2 = ds.create_draft(db, ds.DraftCreate(template_key="status_inquiry"))
    d2.to_address, d2.status, d2.sent_at = "x@example.org", "sent", utcnow() - timedelta(days=10)
    d2.follow_up_due_at, d2.reply_received_at = utcnow() - timedelta(days=1), utcnow()
    db.commit()
    assert ds.due_followups(db) == []
    db.commit()
    db.expire_all()
    assert db.get(EmailDraft, d2.id).follow_up_due_at is not None, "reading must not write"
    run = scheduler.run_task(settings, app.state.sessionmaker, "followup_check", scheduler.TASKS["followup_check"](settings))
    assert run.status == "success"
    db.expire_all()
    assert db.get(EmailDraft, d2.id).follow_up_due_at is None


# ---- CLI ------------------------------------------------------------------------------------

def _cli(tmp_path, *args, data="cli-data", extra_env=None):
    env = {"JOBPILOT_DATA_DIR": str(tmp_path / data), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           **(extra_env or {})}
    return subprocess.run([sys.executable, "-m", "jobpilot.cli", *args], capture_output=True, text=True, env=env,
                          timeout=120, cwd=tmp_path)


def test_cli_quiet_and_clear_errors(tmp_path):
    r = _cli(tmp_path, "init")
    assert r.returncode == 0
    assert "alembic" not in r.stderr.lower() and "INFO" not in r.stderr, r.stderr
    r = _cli(tmp_path, "serve", "--host", "0.0.0.0", "--port", "8799")
    assert r.returncode == 2 and "Traceback" not in r.stderr and "Refusing to bind" in r.stderr
    r = _cli(tmp_path, "add-url", "http://127.0.0.1:1/job")
    assert r.returncode == 1 and "Traceback" not in r.stderr and "local/private" in r.stderr


def test_cli_refuses_to_silently_create_a_new_database(tmp_path):
    r = _cli(tmp_path, "export", "jobs", data="nowhere")
    assert r.returncode == 2 and "jobpilot init" in r.stderr and "Traceback" not in r.stderr
    assert not (tmp_path / "nowhere" / "jobpilot.db").exists()
    r = _cli(tmp_path, "serve", "--port", "8799", data="nowhere")
    assert r.returncode == 2 and not (tmp_path / "nowhere" / "jobpilot.db").exists()


def test_env_file_override(tmp_path):
    envfile = tmp_path / "custom.env"
    envfile.write_text(f"JOBPILOT_DATA_DIR={tmp_path / 'from-envfile'}\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "JOBPILOT_ENV_FILE": str(envfile)}
    r = subprocess.run([sys.executable, "-m", "jobpilot.cli", "init"], capture_output=True, text=True, env=env,
                       timeout=120, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "from-envfile" / "jobpilot.db").exists()
    assert str(tmp_path / "from-envfile") in r.stdout


def test_template_path_independent_of_cwd():
    from jobpilot.web.deps import TEMPLATES_DIR

    assert (TEMPLATES_DIR / "base.html").is_file() and TEMPLATES_DIR.is_absolute()
    assert json.loads((REPO / "tests" / "fixtures" / "lever_postings.json").read_text())
