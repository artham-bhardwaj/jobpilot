"""Milestone 45: draft an email from a LinkedIn hiring post you pasted (synthetic posts only; LinkedIn is never opened)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from jobpilot.models import Contact, DoNotContact, EmailDraft, Job
from jobpilot.services import hiring_posts as hp

POST = """Feed post number 4
Priya Sharma
Priya Sharma
 • 2nd
Senior Talent Acquisition Partner at Examplecorp
3d •
We're hiring! Examplecorp is hiring a Backend Engineer (Java, Spring Boot) for our payments team.
Location: Bengaluru, India
Experience: 1-3 years.
Interested folks can send their resume to priya.sharma [at] examplecorp [dot] com
#hiring #java
"""


def test_parse_reads_poster_role_company_and_obfuscated_email():
    p = hp.parse_post(POST)
    assert p["poster_name"] == "Priya Sharma"
    assert p["poster_title"] == "Senior Talent Acquisition Partner at Examplecorp"
    assert p["role_type"] == "recruiter"
    assert p["company_name"] == "Examplecorp"
    assert p["role_title"] == "Backend Engineer"
    assert p["location"].startswith("Bengaluru")
    assert p["emails"] == ["priya.sharma@examplecorp.com"]
    assert p["warnings"] == []


def test_parse_warns_without_email_and_when_the_post_says_dm_only():
    p = hp.parse_post("Jordan Lee\nEngineering Manager at Synthco\nWe are looking for a Software Engineer. "
                      "DM only, please, or apply via the link.")
    assert p["email"] is None and p["role_type"] == "hiring_manager"
    assert p["company_name"] == "Synthco"
    assert len(p["warnings"]) == 2


def test_company_falls_back_to_the_email_domain_but_not_personal_domains():
    assert hp.parse_post("Openings for Java Developers, mail jobs@acmepay.co.in now please")["company_name"] == "Acmepay"
    assert hp.parse_post("Openings for Java Developers, mail someone@gmail.com now please")["company_name"] is None
    assert hp.parse_post("Hiring alert!\nRole: Software Engineer\nCompany: Zeta Labs\nMail hr@zeta.tech")["company_name"] == "Zeta Labs"


@pytest.mark.parametrize("url, expected", [
    ("https://www.linkedin.com/posts/priya_hiring-activity-123/?utm_source=share", "https://www.linkedin.com/posts/priya_hiring-activity-123"),
    ("linkedin.com/feed/update/urn:li:activity:123", "https://www.linkedin.com/feed/update/urn:li:activity:123"),
    ("", None),
])
def test_post_url_is_cleaned(url, expected):
    assert hp.clean_post_url(url) == expected


def test_post_url_must_be_linkedin():
    with pytest.raises(ValueError):
        hp.clean_post_url("https://evil.example.com/linkedin.com")


def _lead(**kw):
    data = {"post_text": POST, "post_url": "https://www.linkedin.com/posts/priya_x-1", "poster_name": "Priya Sharma",
            "poster_title": "Talent Acquisition at Examplecorp", "company_name": "Examplecorp",
            "role_title": "Backend Engineer", "email": "priya.sharma@examplecorp.com", "email_in_post": True}
    return hp.PostLeadIn.model_validate({**data, **kw})


def test_create_lead_saves_job_contact_and_an_unsent_draft(db):
    res = hp.create_lead(db, _lead())
    db.commit()
    job, contact, draft = res["job"], res["contact"], res["draft"]
    assert job.title == "Backend Engineer" and job.company_name == "Examplecorp"
    assert contact.source_type == "linkedin_manual" and contact.email_status == "unverified"
    assert contact.source_url == "https://www.linkedin.com/posts/priya_x-1"
    assert draft.status == "draft" and draft.sent_at is None
    assert draft.to_address == "priya.sharma@examplecorp.com" and draft.job_id == job.id
    assert "Backend Engineer" in draft.subject and draft.body.startswith("Hi Priya")


def test_create_lead_needs_confirmation_and_respects_do_not_contact(db):
    with pytest.raises(ValueError, match="published"):
        hp.create_lead(db, _lead(email_in_post=False))
    db.add(DoNotContact(kind="domain", value="examplecorp.com", reason="asked not to"))
    db.flush()
    with pytest.raises(ValueError):
        hp.create_lead(db, _lead())


def test_create_lead_without_email_saves_lead_only(db):
    res = hp.create_lead(db, _lead(email=None, email_in_post=False))
    assert res["draft"] is None and res["contact"].email is None


def test_web_flow_parse_then_create(client, db):
    assert "Draft an email from a hiring post" in client.get("/drafts/from-post").text
    page = client.post("/drafts/from-post/parse", data={"post_text": POST, "post_url": ""}).text
    assert 'value="priya.sharma@examplecorp.com"' in page and 'value="Examplecorp"' in page
    r = client.post("/drafts/from-post", data={
        "post_text": POST, "post_url": "", "role_title": "Backend Engineer", "company_name": "Examplecorp",
        "poster_name": "Priya Sharma", "poster_title": "Talent Acquisition at Examplecorp", "role_type": "recruiter",
        "email": "priya.sharma@examplecorp.com", "template_key": "recruiter_outreach", "email_in_post": "1"},
        follow_redirects=False)
    assert r.status_code in (302, 303) and "/drafts/" in r.headers["location"]
    db.expire_all()
    assert db.scalar(select(EmailDraft)).status == "draft"
    assert db.scalar(select(Contact)).name == "Priya Sharma"
    # Without the confirmation box nothing is saved.
    r = client.post("/drafts/from-post", data={"post_text": POST, "role_title": "SDE", "company_name": "Other Co",
                                               "email": "a@otherco.com", "role_type": "recruiter",
                                               "template_key": "recruiter_outreach"})
    assert r.status_code == 400 and "published" in r.text
    db.expire_all()
    assert db.scalar(select(Job).where(Job.company_name == "Other Co")) is None


def test_bad_link_is_refused_on_parse(client):
    r = client.post("/drafts/from-post/parse", data={"post_text": POST, "post_url": "https://example.com/x"})
    assert r.status_code == 400 and "linkedin.com" in r.text
