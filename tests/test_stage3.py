"""Stage 3: contacts, provenance, do-not-contact, email drafts and follow-ups."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from jobpilot.models import Contact, EmailDraft, utcnow
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from jobpilot.services import jobs as job_svc
from tests.helpers import seed_profile


def _job(db, desc="Requirements: Java, Spring Boot, SQL. 1+ years of experience. Contact jane.recruiter@example.com"):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", company_name="Examplecorp",
                                                        location="Bengaluru", description_text=desc,
                                                        source_url="https://careers.example.com/jobs/1"))
    return job


def _contact(db, **kw):
    data = {"name": "Jane Recruiter", "company_name": "Examplecorp", "email": "jane.recruiter@example.com",
            "source_type": "company_website", "source_url": "https://example.com/team"} | kw
    return cs.save_contact(db, cs.ContactIn(**data))[0]


def test_contacts_dedup_and_merge(db):
    c1 = _contact(db)
    c2, merged = cs.save_contact(db, cs.ContactIn(email="Jane.Recruiter@example.com", title="Talent Partner",
                                                  name="Someone Else"))
    assert merged and c1.id == c2.id
    assert c2.name == "Jane Recruiter", "merge must not overwrite existing values"
    assert c2.title == "Talent Partner", "merge fills empty fields"
    c3, merged3 = cs.save_contact(db, cs.ContactIn(name="jane recruiter", company_name="Examplecorp Inc."))
    assert merged3 and c3.id == c1.id


def test_guessed_never_verified(db):
    c = _contact(db, email="j.r@example.com", email_status="guessed", name="J R")
    assert c.email_status == "guessed" and c.confidence <= 0.2 and c.verified_at is None
    with pytest.raises(ValueError):
        cs.save_contact(db, cs.ContactIn(name="X", email="x@example.com", email_status="verified"))
    with pytest.raises(ValueError):
        cs.verify_email(db, c, "")
    cs.verify_email(db, c, "they replied from this address")
    assert c.email_status == "verified" and c.confidence == 1.0


def test_referral_requires_relationship(db):
    with pytest.raises(ValueError):
        cs.save_contact(db, cs.ContactIn(name="Friend", role_type="referral", email="f@example.com"))


def test_contact_validation():
    with pytest.raises(ValidationError):
        cs.ContactIn(name="x", email="bad")
    with pytest.raises(ValidationError):
        cs.ContactIn(name="x", source_url="javascript:alert(1)")


def test_contacts_from_posting(db):
    seed_profile(db)
    job = _job(db)
    created = cs.contacts_from_job(db, job)
    assert len(created) == 1
    c = created[0]
    assert c.source_type == "job_posting" and c.email_status == "unverified" and c.job_id == job.id
    assert "job posting" in c.source_note


def test_dnc_blocks_drafting_and_flags_contacts(db):
    seed_profile(db)
    job = _job(db)
    c = _contact(db)
    cs.add_dnc(db, cs.DncIn(kind="domain", value="@example.com", reason="asked not to be contacted"))
    db.refresh(c)
    assert c.do_not_contact
    with pytest.raises(ds.DraftError):
        ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id))


def test_draft_uses_only_verified_facts_and_marks_missing(db):
    seed_profile(db, confirm=False)
    job = _job(db)
    c = _contact(db, name=None, email="hr@example.com")
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id))
    assert "[[MISSING: candidate.full_name" in d.body, "unconfirmed profile must not be used"
    assert "Test Candidate" not in d.body
    assert "Hi there" in d.body
    with pytest.raises(ds.DraftError):
        ds.approve(db, d)


def test_draft_lifecycle_followups_and_reply(db):
    seed_profile(db)
    job = _job(db)
    c = _contact(db)
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id))
    assert d.missing_fields == [] and "Hi Jane" in d.body and "Test Candidate" in d.body
    assert "java" in d.body.lower() and d.to_address == "jane.recruiter@example.com"
    with pytest.raises(ds.DraftError):
        ds.mark_sent(db, d)  # must be approved first
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject=d.subject, body=d.body + "\nP.S. [[MISSING: x]]"))
    assert d.missing_fields == ["x"]
    with pytest.raises(ds.DraftError):
        ds.approve(db, d)
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject=d.subject, body=d.body.replace("\nP.S. [[MISSING: x]]", "")))
    ds.approve(db, d)
    assert d.status == "approved"
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject=d.subject + "!", body=d.body))
    assert d.status == "draft", "edits after approval require re-approval"
    ds.approve(db, d)
    ds.mark_sent(db, d)
    assert d.follow_up_due_at and d.contact.interactions
    assert ds.due_followups(db) == []
    d.follow_up_due_at = utcnow() - timedelta(minutes=1)
    assert ds.due_followups(db) == [d]
    fu = ds.create_followup(db, d)
    assert fu.kind == "follow_up" and fu.follow_up_of_id == d.id and "Re: " in fu.subject
    ds.record_reply(db, d, "Thanks, let's talk")
    assert fu.status == "cancelled" and fu.suppressed_reason == "reply received"
    assert c.email_status == "verified", "a reply verifies the address"
    with pytest.raises(ds.DraftError):
        ds.create_followup(db, d)


def test_followup_limit(db):
    seed_profile(db)
    job = _job(db)
    c = _contact(db)
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id))
    ds.approve(db, d)
    ds.mark_sent(db, d)
    current = d
    for _ in range(2):
        fu = ds.create_followup(db, current)
        ds.approve(db, fu)
        ds.mark_sent(db, fu)
        current = fu
    assert current.follow_up_due_at is None, "no more follow-ups after the configured maximum"
    with pytest.raises(ds.DraftError):
        ds.create_followup(db, current)


def test_referral_template_requires_referral_contact(db):
    seed_profile(db)
    job = _job(db)
    recruiter = _contact(db)
    with pytest.raises(ds.DraftError):
        ds.create_draft(db, ds.DraftCreate(template_key="referral_request", job_id=job.id, contact_id=recruiter.id))
    friend, _ = cs.save_contact(db, cs.ContactIn(name="Alex Example", role_type="referral", email="alex@example.org",
                                                 relationship_note="Worked together at Synthetic Corp"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="referral_request", job_id=job.id, contact_id=friend.id))
    assert "Worked together" not in d.body, "relationship note is a private reminder, not inserted text"
    assert "Hi Alex" in d.body


def test_template_sandbox():
    with pytest.raises(Exception):
        ds.render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


# ---- Web -------------------------------------------------------------------------

def test_outreach_web_flow(client, db):
    seed_profile(db)
    job = _job(db)
    db.commit()
    r = client.post(f"/jobs/{job.id}/contacts-from-posting")
    assert r.status_code == 303 and "1+contact" in r.headers["location"].replace("%20", "+")
    c = db.scalar(select(Contact))
    assert client.get("/contacts").status_code == 200
    assert client.get(f"/contacts/{c.id}").status_code == 200
    r = client.post("/drafts", data={"template_key": "recruiter_outreach", "job_id": job.id, "contact_id": c.id})
    assert r.status_code == 303
    d = db.scalar(select(EmailDraft))
    page = client.get(f"/drafts/{d.id}").text
    assert "Copy email" in page and "unverified" in page
    assert client.post(f"/drafts/{d.id}/approve").status_code == 303
    db.refresh(d)
    assert d.status == "approved"
    client.post(f"/drafts/{d.id}/sent")
    db.refresh(d)
    assert d.status == "sent" and d.sent_via == "manual"
    assert client.get("/drafts").status_code == 200
    assert client.get("/drafts/templates").status_code == 200
    r = client.post(f"/drafts/templates/{d.template_id}", data={"subject": "{% bad", "body": "x"})
    assert "err=" in r.headers["location"]
    assert "Contacts &amp; outreach" in client.get(f"/jobs/{job.id}").text
    r = client.post("/dnc", data={"kind": "company", "value": "Examplecorp"})
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Contact, c.id).do_not_contact
