"""Milestone 8 — Stage G: recruiter associations, duplicate detection, safe merge, companies, draft revisions."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jobpilot.models import AuditLog, Contact, ContactInteraction, ContactJob, DraftRevision, EmailDraft
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from jobpilot.services import jobs as job_svc
from tests.helpers import seed_profile


def _job(db, title="Java Backend Engineer", company="Examplecorp"):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name=company, location="Bengaluru",
                                                        description_text="Requirements:\n- Java\n- SQL\nContact jobs@example.com"))
    return job


def test_associations_created_from_job_contact_posting_and_drafts(db):
    seed_profile(db)
    job = _job(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp",
                                            job_id=job.id, source_type="company_website",
                                            source_url="https://example.com/team"))
    posted = cs.contacts_from_job(db, job)
    other_job = _job(db, "Python Developer")
    ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=other_job.id, contact_id=c.id))
    links = {(l.contact_id, l.job_id): l.source for l in db.scalars(select(ContactJob))}
    assert links[(c.id, job.id)] == "entered for this job"
    assert links[(posted[0].id, job.id)] == "address published in the posting"
    assert links[(c.id, other_job.id)].startswith("email draft #")
    assert [j.id for j, _, _ in cs.jobs_for_contact(db, c)] == [job.id, other_job.id]
    cs.link_contact_job(db, c, job)  # idempotent
    assert db.query(ContactJob).filter_by(contact_id=c.id, job_id=job.id).count() == 1


def test_duplicate_candidates_and_safe_merge(db):
    seed_profile(db)
    job = _job(db)
    a, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", company_name="Examplecorp", title="Talent Partner",
                                            source_type="public_profile", source_url="https://example.org/jane"))
    b = Contact(name="jane recruiter", company_id=a.company_id, email="jane@example.com", email_status="unverified",
                title="Recruiter", source_type="company_website", do_not_contact=True, dnc_reason="asked not to be contacted")
    db.add(b)
    db.flush()
    cs.log_interaction(db, b, "email", "inbound", "synthetic note")
    cs.link_contact_job(db, b, job)
    db.add(EmailDraft(kind="recruiter_outreach", contact_id=b.id, subject="s", body="b", status="draft"))
    db.flush()
    pairs = cs.duplicate_candidates(db)
    assert len(pairs) == 1 and "same name at the same company" in pairs[0]["reasons"]
    assert ("title", "Talent Partner", "Recruiter") in pairs[0]["conflicts"]
    res = cs.merge_contacts(db, a, b)
    db.flush()
    assert db.get(Contact, b.id) is None
    assert a.title == "Talent Partner", "kept record wins conflicts"
    assert a.email == "jane@example.com" and "email" in res["filled"], "blanks are filled"
    assert a.do_not_contact and a.dnc_reason == "asked not to be contacted", "DNC is never lost"
    assert db.scalar(select(ContactInteraction)).contact_id == a.id
    assert db.scalar(select(EmailDraft)).contact_id == a.id
    assert db.scalar(select(ContactJob)).contact_id == a.id
    assert db.scalar(select(AuditLog).where(AuditLog.action == "contact.merged_records"))
    with pytest.raises(ValueError):
        cs.merge_contacts(db, a, a)


def test_merge_never_makes_an_address_verified_without_a_note(db):
    a = Contact(name="A Person", email="a@example.com", email_status="unverified", source_type="manual")
    b = Contact(name="A Person", email_status="verified", source_type="manual")
    db.add_all([a, b])
    db.flush()
    cs.merge_contacts(db, a, b)
    assert a.email_status == "unverified"


def test_draft_revision_history(db):
    seed_profile(db)
    job = _job(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id))
    original = d.body
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject=d.subject, body=d.body))  # no change
    assert db.scalar(select(DraftRevision)) is None
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject="New subject", body="Edited body"))
    ds.approve(db, d)
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject="New subject", body="Edited again"))
    revs = ds.revisions(db, d)
    assert [r.revision_no for r in revs] == [2, 1]
    assert revs[1].body == original and revs[0].status_before == "approved" and d.status == "draft"


def test_recruiter_web_pages(client, db):
    seed_profile(db)
    job = _job(db)
    a, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", company_name="Examplecorp", title="TP"))
    db.add(Contact(name="Jane Recruiter", company_id=a.company_id, source_type="manual"))
    db.commit()
    assert "same name at the same company" in client.get("/contacts/duplicates").text
    b = db.scalar(select(Contact).where(Contact.id != a.id))
    r = client.post("/contacts/merge", data={"keep_id": a.id, "other_id": b.id})
    assert "err=" in r.headers["location"], "merge needs explicit confirmation"
    r = client.post("/contacts/merge", data={"keep_id": a.id, "other_id": b.id, "confirm": "1"})
    assert f"/contacts/{a.id}" in r.headers["location"]
    client.post(f"/contacts/{a.id}/link-job", data={"job_id": job.id, "relation": "recruiter"})
    assert job.title in client.get(f"/contacts/{a.id}").text
    assert "Jane Recruiter" in client.get(f"/jobs/{job.id}").text
    page = client.get("/companies").text
    assert "Examplecorp" in page
    co = client.get(f"/companies/{a.company_id}").text
    assert job.title in co and "Jane Recruiter" in co
    assert "err=" in client.post(f"/companies/{a.company_id}", data={"website": "javascript:alert(1)"}).headers["location"]
    assert "msg=" in client.post(f"/companies/{a.company_id}", data={"website": "https://example.com"}).headers["location"]
    assert client.get("/companies/999999").status_code == 404
    client.post(f"/contacts/{a.id}/link-job", data={"job_id": job.id, "unlink": "1"})
    db.expire_all()
    assert db.scalar(select(ContactJob)) is None
