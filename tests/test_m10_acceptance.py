"""Milestone 10 acceptance: one synthetic end-to-end workflow mapped to the 20 acceptance criteria.

Offline and repeatable: the Greenhouse board is the synthetic fixture served by a fake HTTP session, DNS is faked,
Gmail is a stub, and the browser step uses headless Chrome against a synthetic page on 127.0.0.1.
Nothing here says anything about live services (see IMPLEMENTATION_STATUS.md for those).
"""

from __future__ import annotations

import base64
import importlib.util
import sqlite3

import pytest
from sqlalchemy import func, select

from jobpilot.models import ApplicationQuestion, Job, SourceRun
from jobpilot.services import answers as answer_svc
from jobpilot.services import applications as apps
from jobpilot.services import backup, queue
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from jobpilot.services import jobs as job_svc
from jobpilot.services import resumes as rs
from tests.helpers import LocalPage, fixture_text, make_http, make_pdf_bytes, seed_profile
from tests.test_m9_validation import _NumberedDraftsStub
from tests.test_stage5 import GoogleStub, gmail_settings  # noqa: F401  (fixture re-export)

GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def test_milestone10_end_to_end(app, db, settings, gmail_settings, monkeypatch):  # noqa: F811
    from jobpilot.integrations import gmail

    seed_profile(db)  # synthetic "Test Candidate": Java/Spring Boot/SQL/REST/Python confirmed, Kafka learning
    src = job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                                   config={"board_token": "examplecorp"}))

    # 1. Import a job from a supported adapter.
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, fixture_text("greenhouse_jobs.json"))}))
    job = db.scalar(select(Job).where(Job.external_id == "1001"))
    senior = db.scalar(select(Job).where(Job.external_id == "1002"))
    assert job and senior

    # 19. Source-run history recorded.
    run = db.scalar(select(SourceRun).where(SourceRun.source_id == src.id))
    assert run.status == "success" and run.new == 2 and run.requests >= 1 and not run.dry_run

    # 2. Parsed and validated.
    assert job.title == "Software Engineer I, Backend (Java)" and "Bengaluru" in job.location
    assert job.apply_url == "https://boards.greenhouse.io/examplecorp/jobs/1001" and job.description_text

    # 3. Deduplicated: the same posting via a tracking-parameter URL is not a new job.
    dup, outcome = job_svc.add_manual(db, job_svc.ManualJobIn(
        title=job.title, company_name="Examplecorp", apply_url="https://boards.greenhouse.io/examplecorp/jobs/1001?gh_src=feed"))
    assert dup.id == job.id and outcome == "duplicate"
    assert db.scalar(select(func.count(Job.id))) == 2

    # 4. Source provenance preserved (API sighting first, manual sighting added, nothing overwritten).
    db.refresh(job)
    assert [s.method for s in job.sightings] == ["api", "manual"]
    assert job.title == "Software Engineer I, Backend (Java)"

    # 5. Explicit vs inferred fields.
    evidence = job.match_details["requirements"]["evidence"]
    assert evidence["seniority"]["basis"] == "inferred"
    skill_ev = job.match_details["skills"]
    assert {"java", "spring boot", "sql"} <= set(skill_ev["mandatory"]) and "kafka" in skill_ev["preferred"]

    # 6. Relevance separate from eligibility; a hard conflict wins over relevance.
    assert job.match_details["relevance"]["score"] == job.relevance_score
    assert job.match_details["eligibility"]["status"] == job.eligibility
    assert senior.hard_conflict and senior.match_details["recommendation"]["key"] == "not_suitable"

    # 7. An approved resume is selected (a draft version never is).
    rs.create_standard_slots(db)
    from jobpilot.models import Resume

    java_slot = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    v = rs.import_version(db, settings, java_slot, "java.pdf", make_pdf_bytes(["Test Candidate", "Java Spring Boot SQL REST APIs"]))
    assert rs.select_for_job(db, job)["version"] is None, "draft versions are never selected"
    rs.approve_version(db, v)
    db.flush()
    sel = rs.select_for_job(db, job)
    assert sel["version"].id == v.id and "kafka" not in sel["coverage"]["required"]

    # 8. The opportunity is a review item.
    assert any(i["job_id"] == job.id for i in queue.build(db))

    # 9. Application packet prepared (persisted snapshot with the selected resume).
    application = apps.create(db, job)
    assert application.resume_version_id == v.id and application.preparation["resume"]["label"] == "Java Backend v1"

    # 10. Application questions recorded; sensitive ones flagged and never suggested from the bank.
    answer_svc.save_entry(db, "Why do you want to work here?", "Synthetic approved answer.")
    qs = answer_svc.add_questions(db, application, ["Why do you want to work here?", "What is your gender?"])
    by_text = {q.question: q for q in qs}
    assert by_text["What is your gender?"].sensitive
    assert answer_svc.suggest(db, "What is your gender?", "java_backend") is None

    # 11. Missing mandatory information blocks readiness.
    for key in ("review_job", "apply_link", "answers"):
        apps.set_check(db, application, key, True)
    with pytest.raises(apps.ApplicationError):
        apps.transition(db, application, "ready_for_review")
    assert application.status == "preparing"

    # 12. Recruiter draft from verified data; 13. revision history kept.
    contact, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    cs.verify_email(db, contact, "synthetic test: address confirmed")  # sending requires a verified recipient
    draft = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=contact.id,
                                               application_id=application.id))
    assert "Test Candidate" in draft.body
    ds.edit_draft(db, draft, ds.DraftEdit(to_address="jane@example.com", subject="Hello", body="First text (synthetic)."))
    ds.approve(db, draft)

    # 14. Stale e-mail content can't be sent: a Gmail draft made before an edit is never the one sent.
    gmail.exchange_code(gmail_settings, "code", "http://127.0.0.1:1/", "v" * 64, http=GoogleStub())
    stub = _NumberedDraftsStub()
    client = gmail.GmailClient(gmail_settings, http=stub)
    gmail.create_gmail_draft(db, gmail_settings, draft, client)
    ds.edit_draft(db, draft, ds.DraftEdit(to_address="jane@example.com", subject="Hello", body="Second text (synthetic)."))
    assert draft.status == "draft" and draft.gmail_draft_id is None
    with pytest.raises(gmail.GmailError):
        gmail.send_via_gmail(db, gmail_settings, draft, client, "SEND")  # re-approval required
    ds.approve(db, draft)
    gmail.send_via_gmail(db, gmail_settings, draft, client, "SEND")
    sent = [kw["json"]["id"] for m, u, kw in stub.calls if u.endswith("/drafts/send")]
    raw = [kw["json"]["message"]["raw"] for m, u, kw in stub.calls if m == "POST" and u.endswith("/drafts")][-1]
    assert sent == ["d-2"] and "Second text" in base64.urlsafe_b64decode(raw + "==").decode(errors="replace")
    assert "First text (synthetic)." in [r.body for r in ds.revisions(db, draft)], "earlier text kept as a revision"

    # 15. Exact-host browser approval; 16. stops before submission.
    from jobpilot.automation import apply as apply_mod

    settings.browser_enabled, settings.browser_headless = True, True
    refused = apply_mod.run_assist(db, settings, application, wait_for_user=False, approved_host="evil.example")
    assert refused.status == "failed" and "Not approved" in refused.message
    if importlib.util.find_spec("playwright"):
        page = LocalPage('<form method=post action=/submitted><label for=fn>First Name *</label>'
                         '<input id=fn name=first_name required><button type=submit>Submit application</button></form>')
        try:
            monkeypatch.setattr(apply_mod, "site_for", lambda url: "greenhouse")
            application.apply_url = page.url
            r = apply_mod.run_assist(db, settings, application, wait_for_user=False, approved_host=page.host)
            if not (r.status == "failed" and "Executable" in (r.message or "")):
                assert r.status in ("filled", "paused")
                assert not [p for p in page.requests if p.startswith("POST")], "the form was never submitted"
        finally:
            page.close()
    assert application.applied_at is None and application.status == "preparing"

    # 17. "Applied" needs approval, attestation and confirmation evidence.
    q = by_text["What is your gender?"]
    answer_svc.set_answer(db, q, "Prefer not to say")
    answer_svc.approve(db, q, answered_myself=True)
    for q in db.scalars(select(ApplicationQuestion).where(ApplicationQuestion.application_id == application.id,
                                                          ApplicationQuestion.status != "approved")):
        answer_svc.set_answer(db, q, q.answer or "Synthetic answer.")
        answer_svc.approve(db, q, answered_myself=True)
    apps.transition(db, application, "ready_for_review")
    apps.approve_submission(db, application)
    with pytest.raises(apps.ApplicationError):
        apps.record_submission(db, application, "confirmation_email", "", attest=True)
    apps.add_evidence(db, settings, application, "confirmation_email", "Thank you for applying (synthetic)")
    db.refresh(application)
    apps.record_submission(db, application, "confirmation_email", "", attest=True)
    assert application.status == "applied" and application.applied_at

    # 20. An anomalous run (empty board) and a failed run expire nothing.
    for body in ('{"jobs": []}', "<html>maintenance</html>"):
        for _ in range(3):
            job_svc.collect_all(db, settings, make_http(settings, {GH: (200, body)}))
    statuses = [r.status for r in db.scalars(select(SourceRun).order_by(SourceRun.id))]
    assert statuses[0] == "success" and set(statuses[1:4]) == {"anomaly"} and set(statuses[4:]) == {"failed"}
    assert all(j.status == "active" for j in db.scalars(select(Job)))

    # 18. Backup and restore preserve everything.
    db.commit()
    path = backup.create_backup(settings, app.state.engine, label="m10-accept")
    db.close()
    backup.restore_backup(settings, path, app.state.engine)
    con = sqlite3.connect(settings.db_file)
    assert con.execute("select status from applications").fetchone()[0] == "applied"
    assert con.execute("select count(*) from jobs where status = 'active'").fetchone()[0] == 2
    assert con.execute("select count(*) from source_runs").fetchone()[0] == len(statuses)
    assert con.execute("select count(*) from draft_revisions").fetchone()[0] >= 2
    assert con.execute("select status from email_drafts").fetchone()[0] == "sent"
    con.close()
