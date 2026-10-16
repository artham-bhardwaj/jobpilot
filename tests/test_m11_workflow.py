"""Milestone 11: reminders, score updates, scheduler backoff and settings, next actions, health.

Synthetic data only; nothing contacts the network.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from jobpilot.models import Application, ApplicationEvent, Job, ReviewItem, TaskRun, utcnow
from jobpilot.services import applications as apps
from jobpilot.services import jobs as job_svc
from jobpilot.services import matching, queue, reminders, scheduler, settings_store
from jobpilot.services import profile as ps
from jobpilot.services import resumes as rs
from tests.helpers import seed_profile


def _job(db, title="Java Backend Engineer", desc="Requirements:\n- Java\n- SQL"):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name="Examplecorp", location="Bengaluru",
                                                        description_text=desc))
    return job


def _items(db, kind):
    return list(db.scalars(select(ReviewItem).where(ReviewItem.kind == kind)))


# ---- Deadline reminders ---------------------------------------------------------------------

def test_deadline_reminder_once_per_deadline_and_respects_dismissal(db):
    job = _job(db)
    job.user_status = "shortlisted"
    job.valid_through = utcnow() + timedelta(days=1)
    db.flush()
    assert reminders.check(db)["deadline_soon"] == 1
    for _ in range(3):  # scheduled runs, restarts, catch-up after the laptop was off
        assert reminders.check(db)["deadline_soon"] == 0
    (item,) = _items(db, "deadline_soon")
    assert item.status == "open" and "Deadline in 1 day(s)" in item.title
    item.status = "dismissed"
    db.flush()
    assert reminders.check(db)["deadline_soon"] == 0, "a dismissed reminder stays dismissed"
    job.valid_through += timedelta(days=1)  # the employer extends the deadline: a new fact
    db.flush()
    assert reminders.check(db)["deadline_soon"] == 1
    assert len(_items(db, "deadline_soon")) == 2


def test_no_deadline_reminder_without_a_stated_deadline_or_when_far_away(db):
    job = _job(db, desc="Requirements:\n- Java\nWe hope to fill this role by December 2030.")
    job.user_status = "shortlisted"
    db.flush()
    assert reminders.check(db)["deadline_soon"] == 0, "vague dates are never treated as deadlines"
    job.valid_through = utcnow() + timedelta(days=20)
    db.flush()
    assert reminders.check(db)["deadline_soon"] == 0


def test_deadline_reminder_resolves_when_job_is_no_longer_watched(db):
    job = _job(db)
    job.user_status = "shortlisted"
    job.valid_through = utcnow() + timedelta(days=1)
    db.flush()
    reminders.check(db)
    job.user_status = "dismissed"
    db.flush()
    reminders.check(db)
    (item,) = _items(db, "deadline_soon")
    assert item.status == "resolved"


def test_explicit_deadline_sentence_counts_and_reminder_shows_in_queue(db):
    soon = date.today() + timedelta(days=1)
    job = _job(db, desc=f"Requirements:\n- Java\nApplications close on {soon:%d %B %Y}.")
    app = apps.create(db, job)
    assert reminders.check(db)["deadline_soon"] == 1
    rows = [i for i in queue.build(db) if i["category"] == "deadline_soon"]
    assert len(rows) == 1 and rows[0]["link"] == f"/applications/{app.id}"
    assert "application preparing" in rows[0]["why"]


# ---- Stale applications and expired listings ----------------------------------------------------

def _submitted(db, days_ago):
    job = _job(db)
    app = Application(job_id=job.id, status="applied", applied_at=utcnow() - timedelta(days=days_ago))
    db.add(app)
    db.flush()
    db.add(ApplicationEvent(application_id=app.id, from_status="ready_for_review", to_status="applied",
                            ts=utcnow() - timedelta(days=days_ago)))
    db.flush()
    return app


def test_stale_application_is_a_check_on_it_reminder_never_a_rejection(db):
    app = _submitted(db, days_ago=30)
    fresh = _submitted(db, days_ago=5)
    assert reminders.check(db)["application_stale"] == 1
    assert reminders.check(db)["application_stale"] == 0
    (item,) = _items(db, "application_stale")
    assert item.entity_id == app.id and "No reply does not mean rejected" in item.reason
    db.refresh(app)
    assert app.status == "applied" and fresh.status == "applied", "status is never changed by a reminder"
    apps.transition(db, app, "interview", "invited")
    reminders.check(db)
    db.refresh(item)
    assert item.status == "resolved"


def test_expired_listing_with_open_application_is_flagged_not_rejected(db):
    job = _job(db)
    app = apps.create(db, job)
    job_svc.mark_expired(db, job, reason="synthetic", actor="system")
    db.flush()
    assert reminders.check(db)["listing_expired"] == 1
    assert reminders.check(db)["listing_expired"] == 0
    assert app.status == "preparing"
    assert "not a rejection" in apps.next_action(db, app)


# ---- Score updates --------------------------------------------------------------------------------

def test_rematch_only_rescoring_jobs_whose_inputs_changed(db):
    seed_profile(db)
    a, b = _job(db), _job(db, title="Python Developer", desc="Requirements:\n- Python")
    db.flush()
    assert matching.rematch_stale(db) == 0, "nothing changed since scoring: ranking is stable"
    before = (a.matched_at, a.relevance_score)
    db.flush()
    past = utcnow() - timedelta(hours=1)
    a.matched_at = b.matched_at = past
    ps.save_fact(db, "skills", ps.SkillIn(name="Rust", status="confirmed"))
    db.flush()
    assert matching.rematch_stale(db) == 2
    assert a.matched_at > past and before[1] is not None
    assert matching.rematch_stale(db) == 0


# ---- Scheduler configuration -----------------------------------------------------------------------

def test_new_tasks_appear_even_with_previously_saved_scheduler_settings(db):
    settings_store.put(db, "scheduler", {"keep_backups": 7, "tasks": {"backup": {"enabled": True, "every_hours": 24}}})
    db.flush()
    cfg = settings_store.get(db, "scheduler")
    assert cfg["keep_backups"] == 7 and cfg["tasks"]["backup"]["enabled"] is True
    for name in ("rematch", "reminders", "health_check", "collect_jobs"):
        assert cfg["tasks"][name]["enabled"] is False, name
    assert cfg["deadline_warning_days"] == 3
    settings_store.validate("scheduler", cfg)
    assert set(scheduler.TASK_LABELS) == set(scheduler.TASKS)


def test_backoff_grows_with_consecutive_failures_and_is_capped(db):
    now = utcnow()
    for i in range(4):
        db.add(TaskRun(task_name="collect_jobs", status="failed", started_at=now - timedelta(seconds=10 - i),
                       finished_at=now - timedelta(seconds=9 - i)))
    db.flush()
    retry = scheduler.backoff_until(db, "collect_jobs", every_hours=24)
    assert timedelta(minutes=119) < retry - now <= timedelta(minutes=120), "15 min x 2^3"
    assert scheduler.backoff_until(db, "collect_jobs", every_hours=1) - now <= timedelta(hours=1)
    db.add(TaskRun(task_name="collect_jobs", status="success", started_at=now, finished_at=now))
    db.flush()
    assert scheduler.backoff_until(db, "collect_jobs", every_hours=24) is None


def test_scheduled_reminders_and_rematch_tasks_run_and_are_idempotent(app, settings, db):
    job = _job(db)
    job.user_status = "shortlisted"
    job.valid_through = utcnow() + timedelta(days=1)
    for name in ("reminders", "rematch", "health_check"):
        cfg = settings_store.get(db, "scheduler")
        cfg["tasks"][name] = {"enabled": True, "every_hours": 12}
        settings_store.put(db, "scheduler", cfg)
    db.commit()
    res = scheduler.run_due(settings, app.state.sessionmaker)
    assert res == {"rematch": "success", "reminders": "success", "health_check": "success"}, res
    assert scheduler.run_due(settings, app.state.sessionmaker) == {}, "nothing is due again straight away"
    db.expire_all()
    assert db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.kind == "deadline_soon")) == 1
    health_run = db.scalar(select(TaskRun).where(TaskRun.task_name == "health_check"))
    assert "ok integrity" in health_run.message


def test_health_includes_backups_and_sources_and_deep_integrity(settings, db):
    from jobpilot.services import health

    res = health.check(settings, db, deep=True)
    assert res["checks"]["integrity"] == {"ok": True, "detail": "ok"}
    assert "no backups yet" in res["checks"]["backups"]["detail"]
    assert res["checks"]["sources"]["detail"] == "no failing sources"
    assert "integrity" not in health.check(settings, db)["checks"], "the /health endpoint stays cheap"


# ---- Preparation: next action, deadline, resume checks ---------------------------------------------

def test_preparation_record_has_next_action_deadline_and_resume_checks(db, settings):
    seed_profile(db, skills=("java",), learning=())
    for e in ps.list_facts(db, "employment"):
        ps.delete_fact(db, "employment", e.id)
    ps.save_fact(db, "employment", ps.EmploymentIn(company="Synthetic Consultancy Services", title="Systems Engineer",
                                                   role_type="testing_qa", start_date=date(2025, 1, 1), status="confirmed",
                                                   description="Integration testing of Java services"))
    ps.save_fact(db, "projects", ps.ProjectIn(name="Course work", kind="academic", technologies="Java", status="confirmed"))
    r = rs.create_resume(db, "Java Backend", "java_backend")
    v = rs.import_version(db, settings, r, "r.txt", b"Systems Engineer, Synthetic Consultancy Services\n"
                                                    b"- Developed Java services\nSKILLS\nJava, SQL\n")
    rs.approve_version(db, v, acknowledged=True)
    job = _job(db)
    job.valid_through = utcnow() + timedelta(days=10)
    app = apps.create(db, job)
    snap = apps.prepare_snapshot(db, app)
    assert snap["deadline"]["date"] == job.valid_through.date().isoformat()
    claims = snap["resume"]["claims"]
    assert len(claims["contradictions"]) == 1 and claims["unconfirmed_job_skills"] == ["sql"]
    kinds = {e["kind"] for e in snap["facts"]["experience"]}
    assert "academic project (not work experience)" in kinds and "work (testing qa)" in kinds
    assert "describes work differently" in snap["next_action"]


def test_next_action_follows_the_lifecycle(db):
    job = _job(db)
    app = Application(job_id=job.id, status="shortlisted")
    db.add(app)
    db.flush()
    assert apps.next_action(db, app).startswith("Start preparing")
    app.status = "ready_for_review"
    assert "approve submission" in apps.next_action(db, app)
    app.submission_approved_at = utcnow()
    assert "Nothing is submitted for you" in apps.next_action(db, app)
    app.status = "applied"
    assert "Silence is not a rejection" in apps.next_action(db, app)


def test_application_page_shows_next_action(client, db):
    job = _job(db)
    app = apps.create(db, job)
    db.commit()
    page = client.get(f"/applications/{app.id}").text
    assert "Next action:" in page and "Deadline" in page


def test_system_page_lists_new_tasks(client):
    page = client.get("/system").text
    for label in ("Score updates", "Deadline and application-status reminders", "System health"):
        assert label in page, label


def test_reminder_kinds_are_dismissable_in_queue(client, db):
    job = _job(db)
    job.user_status = "shortlisted"
    job.valid_through = utcnow() + timedelta(days=1)
    db.flush()
    reminders.check(db)
    db.commit()
    page = client.get("/review?category=deadline_soon").text
    assert "Deadlines approaching" in page and "dismiss" in page
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "deadline_soon"))
    r = client.post(f"/review/{item.id}", data={"status": "dismissed"})
    assert r.status_code == 303
    db.expire_all()
    assert db.get(ReviewItem, item.id).status == "dismissed"
    assert db.scalar(select(func.count(Job.id))) == 1


# ---- Interview preparation and the learning checklist ----------------------------------------

def test_prep_pack_labels_experience_truthfully_and_asks_about_real_projects(db):
    from jobpilot.services import interviews as iv_svc

    seed_profile(db, skills=("java", "sql"), learning=("spring boot",))
    for e in ps.list_facts(db, "employment"):
        ps.delete_fact(db, "employment", e.id)
    ps.save_fact(db, "employment", ps.EmploymentIn(company="Synthetic Consultancy Services", title="Systems Engineer",
                                                   role_type="testing_qa", start_date=date(2025, 1, 1), status="confirmed"))
    ps.save_fact(db, "projects", ps.ProjectIn(name="Synthetic Ledger", kind="personal", technologies="Java, SQL",
                                              status="confirmed"))
    job = _job(db, desc="Requirements:\n- Java\n- Spring Boot\n- REST APIs\n- Kafka")
    app = Application(job_id=job.id, status="applied", applied_at=utcnow())
    db.add(app)
    db.flush()
    iv = iv_svc.save(db, iv_svc.InterviewIn(application_id=app.id, stage="technical"))
    c = iv_svc.generate_prep_pack(db, iv).content
    titles = [s["title"] for s in c["sections"]]
    assert "REST APIs & backend engineering" in titles and "Java" in titles
    stories = " | ".join(c["stories"])
    assert "present it as testing qa work, not as software development" in stories
    assert "Personal project — Synthetic Ledger: not production experience" in stories
    assert any(q.startswith("Synthetic Ledger: why Java, SQL?") for q in c["project_questions"])
    assert c["job_skills_learning"] == ["spring boot"] and "kafka" in c["job_skills_missing"]


def test_learning_checklist_weighs_interview_relevance_and_effort(db):
    from jobpilot.services import skill_gaps

    seed_profile(db, skills=("python",), learning=())
    for i in range(3):
        j = _job(db, title=f"Java Backend Engineer {i}", desc="Requirements:\n- Java\n- SQL\n- Maven")
        j.user_status = "shortlisted"
    db.flush()
    gaps = {g["skill"]: g for g in skill_gaps.analyze(db, scope="shortlisted")["gaps"]}
    assert gaps["sql"]["interview_relevance"] == "high" and gaps["maven"]["interview_relevance"] == "low"
    assert gaps["java"]["effort"] == "months" and gaps["maven"]["effort"] == "days"
    order = [g["skill"] for g in skill_gaps.analyze(db, scope="shortlisted")["gaps"]]
    assert order.index("sql") < order.index("maven"), "same demand: interview-relevant skills first"
    assert all(g["status"] != "confirmed" for g in gaps.values())


# ---- `jobpilot doctor` -------------------------------------------------------------------------

def test_doctor_is_read_only_and_reports_pending_migration_and_health(tmp_path):
    import hashlib
    import subprocess
    import sys

    from tests.test_m9_validation import _cli, _schema5_db

    settings = _schema5_db(tmp_path)
    digest = hashlib.sha256(settings.db_file.read_bytes()).hexdigest()
    r = _cli(tmp_path, "doctor")
    assert r.returncode == 0, r.stderr[-500:]
    assert "Schema:         0005" in r.stdout and "migration pending" in r.stdout
    assert hashlib.sha256(settings.db_file.read_bytes()).hexdigest() == digest, "doctor never migrates"
    assert not list(settings.backup_dir.glob("*.zip"))
    assert _cli(tmp_path, "db", "upgrade").returncode == 0
    r = _cli(tmp_path, "doctor")
    assert r.returncode == 0 and "[ok] integrity: ok" in r.stdout and "[ok] database" in r.stdout
    empty = tmp_path / "nothing"
    r = subprocess.run([sys.executable, "-m", "jobpilot", "doctor"], capture_output=True, text=True, timeout=60,
                       env={"JOBPILOT_DATA_DIR": str(empty), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 2 and "jobpilot init" in r.stderr and not empty.exists()
