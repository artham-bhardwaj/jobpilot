"""Milestone 8 — Stage F: review queue, application preparation record, answer bank (offline; synthetic)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from jobpilot.models import Application, ApplicationQuestion, AuditLog, Job, ReviewItem, TaskRun, utcnow
from jobpilot.services import answers as answer_svc
from jobpilot.services import applications as apps
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from jobpilot.services import jobs as job_svc
from jobpilot.services import queue
from jobpilot.services import resumes as rs
from tests.helpers import make_pdf_bytes, seed_profile


def _job(db, title, desc, **kw):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name=kw.pop("company", "Examplecorp"),
                                                        location=kw.pop("location", "Bengaluru"), description_text=desc,
                                                        remote_type=kw.pop("remote_type", "hybrid"), **kw))
    return job


STRONG = "Requirements:\n- 1+ years of experience with Java\n- Spring Boot\n- SQL\n- REST APIs"


def _cats(db):
    return {i["category"] for i in queue.build(db)}


def test_queue_categories_reasons_and_actions(db, settings):
    seed_profile(db)
    strong = _job(db, "Java Backend Engineer", STRONG)
    unknown = _job(db, "Backend Software Engineer", "Requirements:\n- Java\n- Spring Boot\n- SQL")
    db.add(TaskRun(task_name="collect_jobs", status="failed", message="synthetic failure"))
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", job_id=strong.id,
                                            company_name="Examplecorp"))
    ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=strong.id, contact_id=c.id))
    db.flush()
    items = {i["key"]: i for i in queue.build(db)}
    s = items[f"job:{strong.id}"]
    assert s["category"] == "strong_match" and s["why"] and s["recommended"] and s["company"] == "Examplecorp"
    assert {a["label"] for a in s["actions"]} >= {"Start application", "Dismiss"}
    assert items[f"job:{unknown.id}"]["category"] in ("eligibility_uncertain", "strong_match", "new_job")
    cats = _cats(db)
    for expected in ("no_suitable_resume", "failed_operation", "draft_review", "contact_verification"):
        assert expected in cats, expected


def test_hard_conflict_jobs_never_appear_as_strong(db):
    seed_profile(db, needs_sponsorship=True)
    job = _job(db, "Java Backend Engineer", STRONG + "\nWe cannot sponsor visas.")
    assert job.hard_conflict
    assert not any(i["job_id"] == job.id and i["category"] == "strong_match" for i in queue.build(db))


def test_missing_profile_info_aggregated_and_ack_survives_restart(db):
    seed_profile(db, years=None)
    from jobpilot.models import Employment

    for e in db.scalars(select(Employment)):
        db.delete(e)
    db.flush()
    for i in range(2):
        _job(db, f"Java Backend Engineer {i}", STRONG.replace("1+", "2+"))
    items = [i for i in queue.build(db) if i["category"] == "missing_profile_info"]
    assert items and "2 relevant job(s)" in items[0]["why"]
    queue.apply_action(db, items[0], "acknowledge")
    db.flush()
    assert not any(i["key"] == items[0]["key"] for i in queue.build(db))
    assert queue.stable_id("total work experience") == queue.stable_id("total work experience")


def test_filter_sort_paginate(db):
    seed_profile(db)
    for i in range(30):
        _job(db, f"Java Backend Engineer {i}", STRONG, company=f"Co{i}")
    r = queue.query(db, category="strong_match", per_page=10, page=2, sort="score")
    assert r["total"] == 30 and len(r["rows"]) == 10 and r["pages"] == 3
    assert queue.query(db, q="Co29")["total"] >= 1
    assert all(i["category"] == "strong_match" for i in r["rows"])


def test_bulk_actions_limited_to_safe_steps(db):
    seed_profile(db)
    j1, j2 = _job(db, "Java Backend Engineer", STRONG), _job(db, "Java Backend Engineer II", STRONG, company="Other")
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j1.id, contact_id=c.id))
    db.flush()
    res = queue.bulk_apply(db, [f"job:{j1.id}", f"job:{j2.id}", f"draft:{d.id}", "job:999999"], "dismiss_job")
    assert set(res["applied"]) == {f"job:{j1.id}", f"job:{j2.id}"}
    assert {k for k, _ in res["refused"]} == {f"draft:{d.id}", "job:999999"}
    assert d.status == "draft", "drafts are never approved or sent in bulk"
    assert j1.user_status == "dismissed" and j2.user_status == "dismissed"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "queue.bulk"))
    with pytest.raises(ValueError):
        queue.apply_action(db, {"category": "submission_approval", "category_label": "x", "key": "stored:1"}, "acknowledge")
    assert "approve" not in queue.BULK_ACTIONS and "send" not in queue.BULK_ACTIONS


def test_review_page_and_bulk_via_web(client, db):
    seed_profile(db)
    j = _job(db, "Java Backend Engineer", STRONG)
    db.commit()
    page = client.get("/review").text
    assert "Strong matches" in page and "Recommended next action" in page and j.title in page
    assert client.get("/review?category=strong_match&sort=newest&page=99&q=zzz").status_code == 200
    r = client.post("/review/bulk", data={"keys": [f"job:{j.id}"], "action": "shortlist"})
    assert "msg=" in r.headers["location"]
    db.expire_all()
    assert db.get(Job, j.id).user_status == "shortlisted"
    assert "err=" in client.post("/review/bulk", data={"keys": [f"job:{j.id}"], "action": "approve"}).headers["location"]
    r = client.post("/review/bulk", data={"keys": ["x"], "action": "shortlist", "next": "https://evil.example/"})
    assert r.headers["location"].startswith("/review")


# ---- Preparation record -------------------------------------------------------------------------

def _prepared(db, settings):
    seed_profile(db)
    rs.create_standard_slots(db)
    r = db.scalar(select(rs.Resume).where(rs.Resume.category == "java_backend"))
    rs.approve_version(db, rs.import_version(db, settings, r, "j.pdf", make_pdf_bytes(["Test Candidate", "Java Spring Boot SQL"])))
    db.flush()
    db.expire_all()
    job = _job(db, "Java Backend Engineer", STRONG, apply_url="https://boards.greenhouse.io/examplecorp/jobs/7?gh_src=x")
    cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    return apps.create(db, job)


def test_preparation_record_persisted_with_all_sections(db, settings):
    app = _prepared(db, settings)
    db.commit()
    db.expire_all()
    p = db.get(Application, app.id).preparation
    assert p and app.prepared_at
    assert p["job"]["title"] == "Java Backend Engineer" and p["job"]["sources"] == ["Manual entry"]
    assert p["application_url_canonical"] == "https://boards.greenhouse.io/examplecorp/jobs/7"
    assert p["match"]["score"] and p["match"]["components"] and p["match"]["recommendation"]
    assert p["resume"]["label"] == "Java Backend v1"
    assert "java" in p["facts"]["skills_matched"] and p["facts"]["experience"]
    assert p["contacts"][0]["name"] == "Jane Recruiter" and "email" not in p["contacts"][0], "no contact details copied"
    assert set(p["checklist"]) >= {"resume", "answers", "apply_link"}
    assert db.scalar(select(AuditLog).where(AuditLog.action == "application.prepared"))


def test_unanswered_questions_block_ready_for_review(db, settings):
    app = _prepared(db, settings)
    answer_svc.add_questions(db, app, ["Why do you want to work here?", "Are you legally authorized to work in India?"])
    for key in ("review_job", "apply_link", "answers"):
        apps.set_check(db, app, key, True)
    with pytest.raises(apps.ApplicationError, match="2 application question"):
        apps.transition(db, app, "ready_for_review")
    qs = list(db.scalars(select(ApplicationQuestion).order_by(ApplicationQuestion.id)))
    motivation, legal = qs
    assert legal.sensitive and not motivation.sensitive
    answer_svc.set_answer(db, motivation, "I build backend systems (synthetic).")
    answer_svc.approve(db, motivation)
    answer_svc.set_answer(db, legal, "Yes")
    with pytest.raises(ValueError, match="sensitive"):
        answer_svc.approve(db, legal)
    answer_svc.approve(db, legal, answered_myself=True)
    apps.transition(db, app, "ready_for_review")
    assert app.status == "ready_for_review" and app.preparation["answers"]["approved"] == 2


def test_answer_bank_suggestions_respect_sensitivity_context_and_similarity(db, settings):
    app = _prepared(db, settings)
    e = answer_svc.save_entry(db, "Why do you want to join our company?", "Synthetic motivation answer.", "motivation",
                              approved=True)
    sens = answer_svc.save_entry(db, "Do you require visa sponsorship?", "No", "general", approved=True)
    py_only = answer_svc.save_entry(db, "Describe your Python experience", "Synthetic.", "experience", ["python_backend"], True)
    draft = answer_svc.save_entry(db, "How did you hear about this position?", "Synthetic.", "general", approved=False)
    assert sens.sensitive and sens.category == "sensitive"
    assert answer_svc.suggest(db, "Why do you want to join this company?", "java_backend")["entry"].id == e.id
    assert answer_svc.suggest(db, "Will you require visa sponsorship now or in future?", "java_backend") is None
    assert answer_svc.suggest(db, "Describe your Python experience", "java_backend") is None
    assert answer_svc.suggest(db, "Describe your Python experience", "python_backend")["entry"].id == py_only.id
    assert answer_svc.suggest(db, "How did you hear about this position?", "java_backend") is None, "drafts not suggested"
    assert answer_svc.suggest(db, "What is your favourite colour?", "java_backend") is None
    q = answer_svc.add_questions(db, app, ["Do you require visa sponsorship?"])[0]
    with pytest.raises(ValueError):
        answer_svc.set_answer(db, q, "", from_bank=sens)
    q2 = answer_svc.add_questions(db, app, ["Why do you want to join this company?"])[0]
    answer_svc.set_answer(db, q2, "", from_bank=e)
    assert q2.answer == e.answer and q2.status == "unanswered" and q2.answer_source == f"bank:{e.id}"
    assert len(answer_svc.add_questions(db, app, ["Why do you want to join this company?"])) == 0, "deduplicated"


def test_prep_and_answer_pages_via_web(client, db, settings):
    app = _prepared(db, settings)
    db.commit()
    page = client.get(f"/applications/{app.id}").text
    assert "Preparation record" in page and "Application questions" in page and "Jane Recruiter" in page
    client.post(f"/applications/{app.id}/questions", data={"questions": "Why us?\nWhat is your expected salary?"})
    qs = list(db.scalars(select(ApplicationQuestion)))
    assert len(qs) == 2 and any(q.sensitive for q in qs)
    sal = next(q for q in qs if q.sensitive)
    client.post(f"/applications/questions/{sal.id}", data={"answer": "Negotiable"})
    r = client.post(f"/applications/questions/{sal.id}", data={"action": "approve"})
    assert "err=" in r.headers["location"]
    assert "msg=" in client.post(f"/applications/{app.id}/prepare").headers["location"]
    assert client.post("/answers", data={"question": "Notice period?", "answer": "30 days (synthetic)", "approved": "1",
                                         "category": "logistics"}).status_code == 303
    assert "Notice period?" in client.get("/answers").text
    assert client.post("/applications/questions/999999", data={"answer": "x"}).status_code == 404


def test_new_pages_are_read_only(client, db, settings):
    import hashlib
    import sqlite3

    app = _prepared(db, settings)
    answer_svc.add_questions(db, app, ["Why us?"])
    db.commit()

    def fp():
        con = sqlite3.connect(settings.db_file)
        try:
            return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
        finally:
            con.close()

    before = fp()
    for url in ("/review", "/review?category=strong_match", "/answers", "/matching", "/matching?w_required_skills=60",
                "/sources", f"/jobs/{app.job_id}/resume-review", f"/applications/{app.id}", "/"):
        assert client.get(url).status_code == 200, url
    assert fp() == before
