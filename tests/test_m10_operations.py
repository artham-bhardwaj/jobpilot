"""Milestone 10: operational regressions (offline, synthetic data only)."""
from __future__ import annotations

import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from jobpilot.models import TaskRun, utcnow
from jobpilot.services.tasks import INTERRUPTED, run_task, task_lock
from tests.helpers import make_http
from tests.test_stage4 import chrome_page  # noqa: F401  (fixture re-export; skips without Chrome)

KILL_MID_TASK = """
import os, signal
from jobpilot.config import Settings
from jobpilot.db import make_engine, make_sessionmaker
from jobpilot.services.tasks import run_task
s = Settings(_env_file=None, data_dir={data!r})
run_task(s, make_sessionmaker(make_engine(s)), "backup", lambda sess: os.kill(os.getpid(), signal.SIGKILL))
"""


def test_task_killed_mid_run_is_reported_as_failed_on_next_run(app, settings, db):
    r = subprocess.run([sys.executable, "-c", KILL_MID_TASK.format(data=str(settings.data_path))], timeout=120)
    assert r.returncode == -9
    db.expire_all()
    stale = db.scalar(select(TaskRun).where(TaskRun.task_name == "backup"))
    assert stale.status == "running" and stale.finished_at is None  # what a crash leaves behind

    run = run_task(settings, app.state.sessionmaker, "backup", lambda s: {"message": "ok"})
    db.expire_all()
    assert run.status == "success"
    stale = db.get(TaskRun, stale.id)
    assert stale.status == "failed" and stale.message == INTERRUPTED and stale.finished_at is not None
    from jobpilot.services import health

    assert health.check(settings, db)["checks"]["last_task_failure"]["detail"].startswith("backup")


def test_startup_marks_only_orphaned_old_runs(settings):
    from jobpilot.db import make_engine, make_sessionmaker, migrate
    from jobpilot.web.app import create_app

    settings.ensure_dirs()
    engine = make_engine(settings)
    migrate(settings, engine)
    old = utcnow() - timedelta(minutes=10)
    with make_sessionmaker(engine)() as s:
        s.add_all([TaskRun(task_name="backup", trigger="scheduled", status="running", started_at=old),
                   TaskRun(task_name="collect_jobs", trigger="scheduled", status="running", started_at=old),
                   TaskRun(task_name="maintenance", trigger="scheduled", status="running", started_at=utcnow())])
        s.commit()
    engine.dispose()
    with task_lock(settings, "collect_jobs"):  # still genuinely running elsewhere
        app = create_app(settings)
    with app.state.sessionmaker() as s:
        status = {r.task_name: r.status for r in s.scalars(select(TaskRun))}
    app.state.engine.dispose()
    assert status == {"backup": "failed", "collect_jobs": "running", "maintenance": "running"}


@pytest.mark.parametrize("retry_after", ["-5", "nan", "Wed, 21 Oct 2015 07:28:00 GMT", "", "1e9"])
def test_odd_retry_after_values_never_crash_the_rate_limit_path(db, settings, monkeypatch, retry_after):
    from jobpilot.services import jobs as job_svc

    slept = []
    monkeypatch.setattr("jobpilot.sources.http.time.sleep", slept.append)
    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", config={"board_token": "examplecorp"}))
    gh = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"
    res = job_svc.collect_all(db, settings, make_http(settings, {gh: (429, "slow down", {"Retry-After": retry_after})}))
    assert res["sources"]["Examplecorp"]["status"] == "unavailable", res
    assert slept and all(0 <= d <= 60 for d in slept)

VALUES = {"first_name": "Test", "email": "test.candidate@example.com"}


@pytest.mark.parametrize("html,reason", [
    ('<form><label for="e">Email</label><input id="e" name="email"><label for="p">Password</label>'
     '<input id="p" type="password" name="password"><button type="submit">Sign in</button></form>', "login required"),
    ('<p>Enter the verification code we sent to your phone</p><form><label for="e">Email</label>'
     '<input id="e" name="email"><input name="otp_code" autocomplete="one-time-code"></form>', "MFA"),
])
def test_assist_pauses_on_login_walls_and_mfa_without_filling(chrome_page, html, reason):  # noqa: F811
    from jobpilot.automation.apply import assist_page

    submitted = []
    chrome_page.on("request", lambda r: submitted.append(r.method) if r.method == "POST" else None)
    chrome_page.set_content(html)
    res = assist_page(chrome_page, VALUES, None)
    assert res.status == "paused" and any(reason in r for r in res.pause_reasons), res.pause_reasons
    assert chrome_page.input_value("#e") == "" and not res.filled
    assert submitted == []


GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def _two_openings(title_b_suffix=" "):
    """Shape seen on a real board: two openings (different ids/requisitions) for the same role and location."""
    def job(i, title):
        return {"id": i, "title": title, "absolute_url": f"https://job-boards.greenhouse.io/examplecorp/jobs/{i}",
                "location": {"name": "Remote, Germany"}, "content": "Requirements: Java, SQL"}
    import json

    return json.dumps({"jobs": [job(101, "Strategic Account Executive"), job(102, "Strategic Account Executive" + title_b_suffix)]})


def test_same_source_openings_with_same_title_stay_separate_and_stable(db, settings):
    from jobpilot.models import Job
    from jobpilot.services import jobs as job_svc

    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                             config={"board_token": "examplecorp"}))
    first = job_svc.collect_all(db, settings, make_http(settings, {GH_BOARD: (200, _two_openings())}))["sources"]["Examplecorp"]
    assert first["new"] == 2 and first["updated"] == 0
    for _ in range(2):
        again = job_svc.collect_all(db, settings, make_http(settings, {GH_BOARD: (200, _two_openings())}))["sources"]["Examplecorp"]
        assert again["unchanged"] == 2 and again["updated"] == 0, again
    jobs = {j.external_id: j for j in db.scalars(select(Job))}
    assert set(jobs) == {"101", "102"}
    assert all(j.apply_url.endswith(f"/jobs/{ext}") for ext, j in jobs.items()), "each job keeps its own apply URL"


def test_cross_source_fingerprint_dedup_still_works(db, settings):
    from jobpilot.models import Job
    from jobpilot.services import jobs as job_svc

    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                             config={"board_token": "examplecorp"}))
    job_svc.collect_all(db, settings, make_http(settings, {GH_BOARD: (200, _two_openings(""))}))
    manual, outcome = job_svc.add_manual(db, job_svc.ManualJobIn(title="Strategic Account Executive",
                                                                 company_name="Examplecorp", location="Remote, Germany"))
    assert outcome == "duplicate" and db.scalar(select(func.count(Job.id))) == 2


# ---- Parsing discrepancies found on the first live board (Milestone 10) ----------------------

@pytest.mark.parametrize("sentence,expected", [
    ("• Professional experience with Go or another backend or systems language.", ["go"]),
    ("• Design, build, and maintain Go-based automation for PostgreSQL.", ["go", "postgresql"]),
    ("We go to market quickly and go beyond expectations.", []),
])
def test_bare_go_is_recognised_only_as_the_language(sentence, expected):
    from jobpilot.services.skills_taxonomy import extract_skills

    assert extract_skills(sentence) == expected


def test_employers_own_name_is_not_a_skill_requirement():
    from jobpilot.services import job_parser

    text = ("Requirements:\n- Experience with Ruby on Rails\n- You will help GitLab customers succeed\n"
            "How GitLab Supports Full-Time Employees\n")
    for company in ("GitLab", "GitLab Inc."):
        p = job_parser.parse("Backend Engineer", text, company=company)
        assert "git" not in p.skill_evidence and p.mandatory_skills == ["ruby"], company
    p = job_parser.parse("Security Engineer", "Requirements:\n- Experience administering Git/GitLab", company="GitLab")
    assert p.mandatory_skills == ["git"], "a real Git requirement still counts"
    other = job_parser.parse("Engineer", "Requirements:\n- Experience with GitLab merge requests", company="Examplecorp")
    assert "git" in other.mandatory_skills, "another employer asking for GitLab experience still counts"
