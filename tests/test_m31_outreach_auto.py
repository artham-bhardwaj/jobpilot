"""Milestone 31: automatic recruiter outreach under standing authorization (fake Gmail, synthetic data).

Nothing reaches a real mailbox: Gmail is the FakeGmail from test_m13.
"""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import func, select

from jobpilot.models import AuditLog, EmailDraft, Job, Notification, ReviewItem, utcnow
from jobpilot.services import notifications, outreach_auto, settings_store
from jobpilot.services import contacts as cs
from tests.helpers import seed_profile
from tests.test_m13_gmail import FakeGmail


@pytest.fixture
def ready(db, settings):
    seed_profile(db)
    settings.gmail_enabled = True
    settings.ensure_dirs()
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    settings.gmail_token_file.write_text(json.dumps({
        "access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600, "email": "test.candidate@example.com",
        "scope": "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.metadata"}))
    settings_store.put(db, "outreach", {**settings_store.get(db, "outreach"), "auto_enabled": True, "auto_send": True})
    db.commit()
    return settings


def _job(db, n=1, company="Examplecorp", **kw):
    j = Job(title=kw.pop("title", f"Java Backend Engineer {n}"), company_name=company, status="active", user_status="new",
            relevance_score=kw.pop("score", 82.0), eligibility=kw.pop("eligibility", "likely_eligible"),
            category="java_backend", fingerprint=f"fp-{n}-{company}", source_url=f"https://job-boards.greenhouse.io/x/jobs/{n}",
            apply_url=f"https://job-boards.greenhouse.io/x/jobs/{n}", first_seen_at=utcnow(), last_seen_at=utcnow(),
            match_details={"skills": {"mandatory": ["java"], "preferred": []}}, **kw)
    db.add(j)
    db.flush()
    return j


def _recruiter(db, job, email="jane@examplecorp.com", status="verified", company="Examplecorp"):
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", title="Technical Recruiter", company_name=company,
                                            job_id=job.id, email=email, email_status=status,
                                            verification_note="listed on the company careers page (synthetic)"
                                            if status == "verified" else None))
    db.commit()
    return c


def _client(settings, fake):
    from jobpilot.integrations import gmail

    return gmail.GmailClient(settings, http=fake)


def test_off_by_default_and_paused_does_nothing(db, settings):
    assert "off" in outreach_auto.run(db, settings)["message"]
    assert settings_store.get(db, "scheduler")["tasks"]["outreach"]["enabled"] is False
    settings_store.put(db, "outreach", {**settings_store.get(db, "outreach"), "auto_enabled": True})
    outreach_auto.set_paused(db, True, "test")
    assert "paused" in outreach_auto.run(db, settings)["message"]


def test_no_contact_queues_recruiter_discovery_once(ready, db):
    j = _job(db)
    _job(db, n=2, score=40)  # below the policy: ignored
    for _ in range(2):
        res = outreach_auto.run(db, ready, client=_client(ready, FakeGmail()))
    assert res["recruiter_needed"] == 1 and res["jobs"] == 1
    items = db.scalars(select(ReviewItem).where(ReviewItem.kind == "recruiter_needed")).all()
    assert len(items) == 1 and items[0].entity_id == j.id and f"--job {j.id}" in items[0].reason
    assert db.scalar(select(func.count(EmailDraft.id))) == 0


@pytest.mark.parametrize("status", ["apollo_reported", "unverified", "guessed"])
def test_unconfirmed_addresses_are_queued_not_sent(ready, db, status):
    j = _job(db)
    _recruiter(db, j, status=status)
    fake = FakeGmail()
    res = outreach_auto.run(db, ready, client=_client(ready, fake))
    assert res["unverified"] == 1 and res["sent"] == 0 and fake.calls == []
    assert db.scalar(select(func.count(EmailDraft.id))) == 0
    assert db.scalar(select(ReviewItem.kind).where(ReviewItem.status == "open")) == "recruiter_unverified"


def test_verified_contact_gets_one_email_per_job_and_a_confirmed_sent_alert(ready, db):
    j = _job(db)
    _recruiter(db, j)
    fake = FakeGmail()
    res = outreach_auto.run(db, ready, client=_client(ready, fake))
    assert res["sent"] == 1 and fake.count("POST", "/drafts/send") == 1
    d = db.scalar(select(EmailDraft))
    assert d.status == "sent" and d.sent_via == "gmail" and d.gmail_message_id and d.to_address == "jane@examplecorp.com"
    assert "I applied" not in d.body and "Java Backend Engineer 1" in d.body and d.job_id == j.id
    again = outreach_auto.run(db, ready, client=_client(ready, fake))
    assert again["already_contacted"] == 1 and fake.count("POST", "/drafts/send") == 1, "never twice"
    assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "outreach.auto_sent")) == 1
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"), "kinds": {"daily_summary": False}})
    notifications.scan(db, ready)
    sent = [n for n in db.scalars(select(Notification)) if n.kind == "outreach_sent"]
    assert len(sent) == 1 and "SENT (Gmail confirmed message" in sent[0].text


def test_daily_cap_and_draft_only_mode(ready, db):
    settings_store.put(db, "outreach", {**settings_store.get(db, "outreach"), "auto_daily_limit": 1})
    a, b = _job(db, n=1), _job(db, n=2)
    _recruiter(db, a)
    c2, _ = cs.save_contact(db, cs.ContactIn(name="Raj Talent", company_name="Examplecorp", job_id=b.id,
                                             email="raj@examplecorp.com", email_status="verified",
                                             verification_note="replied from this address (synthetic)"))
    db.commit()
    fake = FakeGmail()
    res = outreach_auto.run(db, ready, client=_client(ready, fake))
    assert res["sent"] == 1 and res["in_gmail_drafts"] == 1 and fake.count("POST", "/drafts/send") == 1
    held = db.scalar(select(EmailDraft).where(EmailDraft.sent_at.is_(None)))
    assert held.gmail_status == "created" and held.status == "approved", "over the cap: Gmail Drafts, not sent"


def test_domain_mismatch_and_send_failure_are_never_marked_sent(ready, db):
    j = _job(db)
    _recruiter(db, j, email="jane@othermail.io")
    fake = FakeGmail()
    res = outreach_auto.run(db, ready, client=_client(ready, fake))
    assert res["review"] == 1 and fake.calls == []
    j2 = _job(db, n=2, company="Examplecorp")
    c, _ = cs.save_contact(db, cs.ContactIn(name="Raj Talent", company_name="Examplecorp", job_id=j2.id,
                                            email="raj@examplecorp.com", email_status="verified",
                                            verification_note="careers page (synthetic)"))
    db.commit()
    fake.fail[("POST", "/drafts/send")] = [(400, {"error": {"code": 400, "message": "Invalid To header"}})]
    res = outreach_auto.run(db, ready, client=_client(ready, fake))
    assert res["failed"] == 1 and res["sent"] == 0
    d = db.scalar(select(EmailDraft).where(EmailDraft.contact_id == c.id))
    assert d.sent_at is None and d.status != "sent"
    assert db.scalar(select(ReviewItem.kind).where(ReviewItem.entity_id == d.id, ReviewItem.status == "open")) == "outreach_failed"


def test_old_drafts_and_bounced_contacts_are_never_sent(ready, db):
    from jobpilot.services import drafts as ds

    j = _job(db)
    c = _recruiter(db, j)
    old = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=c.id))
    ds.approve(db, old)  # an approved draft from earlier: the task must not send it
    j2 = _job(db, n=2)
    bounced = _recruiter(db, j2, email="gone@examplecorp.com")
    cs.record_bounce(db, bounced, "synthetic")
    db.commit()
    fake = FakeGmail()
    res = outreach_auto.run(db, ready, client=_client(ready, fake))
    assert res["already_contacted"] == 1 and res["recruiter_needed"] == 1 and res["sent"] == 0
    assert fake.count("POST", "/drafts/send") == 0
    db.refresh(old)
    assert old.sent_at is None and old.status == "approved"


def test_scheduler_runs_outreach_after_rematch_and_before_notify():
    from jobpilot.services import scheduler

    order = list(scheduler.TASKS)
    assert order.index("rematch") < order.index("outreach") < order.index("notify")
    assert "autopilot" not in order and "tailor_resumes" not in order, "no auto-apply, no background resumes"
