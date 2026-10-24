"""Milestone 30: correcting or removing placeholder recruiter contacts and cancelling outreach drafts (synthetic data).

Nothing is sent: cancelled drafts can no longer be approved, put in Gmail or sent.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from jobpilot.models import AuditLog, Contact, ContactJob, EmailDraft, Job, utcnow
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from tests.helpers import seed_profile


def _job(db, title="Software Engineer", company="Rubrik"):
    j = Job(title=title, company_name=company, status="active", user_status="new", relevance_score=77,
            eligibility="likely_eligible", category="general_swe", fingerprint=f"fp-{title}", first_seen_at=utcnow(),
            last_seen_at=utcnow(), source_url="https://job-boards.greenhouse.io/x/jobs/1")
    db.add(j)
    db.flush()
    return j


def _placeholder(db, job):
    c, _ = cs.save_contact(db, cs.ContactIn(name="Actual Recruiter Name", title="Technical Recruiter", company_name="Rubrik",
                                            job_id=job.id, linkedin_url="https://www.linkedin.com/in/actual-profile",
                                            source_type="linkedin_manual"))
    db.commit()
    return c


def test_placeholder_contact_is_removed_with_its_job_link_and_an_audit_record(db):
    j = _job(db)
    c = _placeholder(db, j)
    cid = c.id
    out = cs.delete_contact(db, c, "test placeholder")
    db.commit()
    assert out["removed"] == cid and out["jobs"] == [j.id] and out["linkedin_url"].endswith("/actual-profile")
    assert db.get(Contact, cid) is None and db.scalar(select(func.count(ContactJob.id))) == 0
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "contact.deleted", AuditLog.entity_id == cid))
    details = entry.details if isinstance(entry.details, dict) else json.loads(entry.details)
    assert details["reason"] == "test placeholder" and details["removed"]["name"] == "Actual Recruiter Name"
    assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "contact.created", AuditLog.entity_id == cid)) == 1, \
        "earlier history is kept"
    assert db.get(Job, j.id) is not None, "the job itself is untouched"


def test_a_contact_with_drafts_is_never_removed(db):
    seed_profile(db)
    j = _job(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", company_name="Rubrik", email="jane@rubrik.com"))
    ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=c.id))
    with pytest.raises(ValueError, match="1 email draft"):
        cs.delete_contact(db, c, "cleanup")
    with pytest.raises(ValueError, match="why"):
        cs.delete_contact(db, _placeholder(db, _job(db, title="Other")), "  ")
    assert db.get(Contact, c.id) is not None


def test_dashboard_delete_uses_the_same_guard(client, db):
    seed_profile(db)
    j = _job(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", company_name="Rubrik", email="jane@rubrik.com"))
    ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=c.id))
    db.commit()
    r = client.post(f"/contacts/{c.id}/delete", follow_redirects=False)
    assert r.status_code == 303 and f"/contacts/{c.id}" in r.headers["location"] and "err=" in r.headers["location"]
    db.expire_all()
    assert db.get(Contact, c.id) is not None


def test_cancelled_draft_can_never_be_approved_put_in_gmail_or_sent(db):
    seed_profile(db)
    j = _job(db, company="Mastercard")
    d = EmailDraft(kind="recruiter_outreach", job_id=j.id, to_address="recruiter@mastercard.com", subject="s",
                   body="b", status="approved", approved_at=utcnow(), missing_fields=[])
    db.add(d)
    db.commit()
    ds.cancel(db, d)
    db.commit()
    assert d.status == "cancelled" and d.sent_at is None
    with pytest.raises(ds.DraftError):
        ds.approve(db, d)
    with pytest.raises(ds.DraftError):
        ds.mark_sent(db, d)
    from jobpilot.integrations import gmail

    with pytest.raises(gmail.GmailError):
        gmail._guard(db, d)  # every Gmail create/send path calls this first
    assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "draft.cancelled")) == 1


def test_cli_dry_run_then_remove_update_and_cancel(settings, monkeypatch, capsys, tmp_path):
    from jobpilot import cli
    from jobpilot.config import get_settings

    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(settings.data_dir))
    get_settings.cache_clear()
    try:
        assert cli.main(["init"]) == 0
        assert cli.main(["recruiters", "add", "--name", "Actual Recruiter Name", "--company", "Rubrik",
                         "--linkedin", "https://www.linkedin.com/in/actual-profile"]) == 0
        capsys.readouterr()  # discard the add output; the id is read from the list below
        assert cli.main(["recruiters", "list"]) == 0
        rows = json.loads(capsys.readouterr().out)
        cid = rows[0]["id"]
        assert cli.main(["recruiters", "update", "--id", str(cid), "--name", "Jane Recruiter",
                         "--linkedin", "https://www.linkedin.com/in/jane-recruiter"]) == 0
        updated = json.loads(capsys.readouterr().out)
        assert updated["name"] == "Jane Recruiter" and updated["linkedin"].endswith("/jane-recruiter")
        assert updated["company"] == "Rubrik", "fields not given are kept"
        assert cli.main(["recruiters", "remove", "--id", str(cid), "--reason", "test placeholder"]) == 0
        assert "would_remove" in json.loads(capsys.readouterr().out), "without --yes nothing is removed"
        assert cli.main(["recruiters", "remove", "--id", str(cid), "--reason", "test placeholder", "--yes"]) == 0
        assert json.loads(capsys.readouterr().out)["removed"] == cid
        assert cli.main(["recruiters", "remove", "--id", str(cid), "--yes", "--reason", "x"]) == 1, "already gone"
        assert cli.main(["outreach", "cancel", "--draft", "999"]) == 1
    finally:
        get_settings.cache_clear()
