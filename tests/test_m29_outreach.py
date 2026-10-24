"""Milestone 29: recruiter contacts (manual LinkedIn/Apollo, optional Apollo API), personalised outreach drafts,
duplicate/bounce protection and Telegram views. Synthetic data, fake Apollo and Gmail; nothing is sent or charged."""
from __future__ import annotations

import json
from datetime import date

import pytest
import requests
from pydantic import SecretStr
from sqlalchemy import func, select

from jobpilot.models import AuditLog, Contact, EmailDraft, EmailTemplate, Job, Notification, utcnow
from jobpilot.services import apollo, notifications, settings_store, telegram_bot
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from tests.helpers import seed_profile


def _job(db, title="Java Backend Engineer", company="Examplecorp", url="https://job-boards.greenhouse.io/x/jobs/1"):
    j = Job(title=title, company_name=company, status="active", user_status="new", relevance_score=80,
            eligibility="likely_eligible", category="java_backend", fingerprint=f"fp-{title}-{url}", source_url=url,
            apply_url=url, first_seen_at=utcnow(), last_seen_at=utcnow(),
            match_details={"skills": {"mandatory": ["java", "spring boot"], "preferred": ["kafka"]}})
    db.add(j)
    db.flush()
    return j


@pytest.fixture
def profile(db):
    from jobpilot.services import profile as ps

    seed_profile(db)
    ps.save_fact(db, "projects", ps.ProjectIn(name="LedgerLite", description="Payments ledger service\nBuilt with Java and "
                                              "Spring Boot (synthetic)", technologies=["Java", "Spring Boot"],
                                              status="confirmed"))
    ps.save_fact(db, "projects", ps.ProjectIn(name="PixelPaint", description="Drawing app in Swift (synthetic)",
                                              technologies=["Swift"], status="confirmed"))
    db.flush()
    return db


# ---- contacts --------------------------------------------------------------------------------------------------

def test_linkedin_contact_entered_by_you_is_deduplicated(db):
    j = _job(db)
    c, merged = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", title="Technical Recruiter", company_name="Examplecorp",
                                                 job_id=j.id, linkedin_url="https://www.linkedin.com/in/jane-recruiter",
                                                 source_type="linkedin_manual"))
    assert not merged and c.source_type == "linkedin_manual" and c.email is None
    again, merged = cs.save_contact(db, cs.ContactIn(name="J. Recruiter", title="Recruiter", company_name="Examplecorp",
                                                     linkedin_url="https://www.linkedin.com/in/jane-recruiter",
                                                     email="jane@examplecorp.com", source_type="apollo_manual",
                                                     email_status="apollo_reported"))
    assert merged and again.id == c.id and again.name == "Jane Recruiter", "merging never overwrites what's there"
    assert again.email == "jane@examplecorp.com" and again.email_status == "apollo_reported"
    assert db.scalar(select(func.count(Contact.id))) == 1


@pytest.mark.parametrize("email,company,warn", [
    ("jane@examplecorp.com", "Examplecorp", None),
    ("jane@gmail.com", "Examplecorp", "personal email domain"),
    ("jane@othercorp.io", "Examplecorp Technologies", "does not mention"),
])
def test_company_domain_inconsistency_is_reported_not_changed(db, email, company, warn):
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane", email=email, company_name=company))
    got = cs.domain_warning(c)
    assert (got is None) if warn is None else (warn in got)
    assert c.email == email, "never changed silently"


def test_apollo_csv_import_records_only_what_apollo_reported(db):
    j = _job(db)
    csv_text = ("First Name,Last Name,Title,Company,Email,Email Status,Person Linkedin Url\n"
                "Jane,Recruiter,Technical Recruiter,Examplecorp,jane@examplecorp.com,Verified,linkedin.com/in/jane-r\n"
                "Raj,Talent,Talent Acquisition Partner,Examplecorp,raj.personal@gmail.com,Verified,\n"
                ",,,,,,\n")
    out = apollo.import_csv(db, csv_text.encode(), job_id=j.id)
    assert out["created"] == 2 and out["skipped"] == 1
    jane = db.scalar(select(Contact).where(Contact.email == "jane@examplecorp.com"))
    assert jane.email_status == "apollo_reported" and jane.source_type == "apollo_manual"
    assert jane.linkedin_url == "https://linkedin.com/in/jane-r" and j.id in [x.id for x, _, _ in cs.jobs_for_contact(db, jane)]
    raj = db.scalar(select(Contact).where(Contact.name == "Raj Talent"))
    assert raj.email is None and any("personal address" in w for w in out["warnings"])
    again = apollo.import_csv(db, csv_text.encode(), job_id=j.id)
    assert again["created"] == 0 and again["merged"] == 2, "re-importing does not duplicate"


# ---- Apollo API (optional; fake HTTP) -----------------------------------------------------------------------------

class FakeApollo(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None, **kw):  # noqa: A002
        self.calls.append((url, json, headers))
        status, body = self.responses.pop(0)
        r = requests.Response()
        r.status_code, r._content = status, (body if isinstance(body, bytes) else __import__("json").dumps(body).encode())
        return r


@pytest.fixture
def apollo_on(db, settings):
    settings.apollo_api_key = SecretStr("apollo-synthetic-key-123")
    settings_store.put(db, "apollo", {**settings_store.get(db, "apollo"), "enabled": True, "max_enrichments_per_day": 1})
    return settings


def test_api_is_off_without_setting_or_key(db, settings):
    assert not apollo.api_status(db, settings)["ready"]
    with pytest.raises(apollo.ApolloError, match="off"):
        apollo.search_recruiters(db, settings, "examplecorp.com", client=FakeApollo([]))
    settings_store.put(db, "apollo", {**settings_store.get(db, "apollo"), "enabled": True})
    with pytest.raises(apollo.ApolloError, match="JOBPILOT_APOLLO_API_KEY"):
        apollo.search_recruiters(db, settings, "examplecorp.com")


def test_search_is_free_and_stores_nothing(db, apollo_on):
    fake = FakeApollo([(200, {"people": [{"id": "p1", "first_name": "Jane", "last_name_obfuscated": "R***",
                                          "title": "Technical Recruiter", "has_email": True,
                                          "organization": {"name": "Examplecorp"}}], "total_entries": 1})])
    client = apollo.ApolloClient(apollo_on, http=fake)
    assert "apollo-synthetic-key" not in repr(client)
    res = apollo.search_recruiters(db, apollo_on, "www.examplecorp.com", client=client)
    url, payload, headers = fake.calls[0]
    assert url.endswith("/api/v1/mixed_people/api_search") and payload["q_organization_domains_list"] == ["examplecorp.com"]
    assert headers["x-api-key"] == "apollo-synthetic-key-123" and "technical recruiter" in payload["person_titles"]
    assert res == [{"apollo_id": "p1", "name": "Jane R***", "title": "Technical Recruiter", "company": "Examplecorp",
                    "has_email": True}]
    assert db.scalar(select(func.count(Contact.id))) == 0, "a search result is not a contact"


def test_enrichment_needs_explicit_credit_consent_and_respects_the_daily_cap(db, apollo_on):
    j = _job(db)
    person = {"person": {"id": "p1", "name": "Jane Recruiter", "title": "Technical Recruiter", "email": "jane@examplecorp.com",
                         "email_status": "verified", "linkedin_url": "https://www.linkedin.com/in/jane-r",
                         "organization": {"name": "Examplecorp"}}}
    fake = FakeApollo([(200, person), (200, person)])
    client = apollo.ApolloClient(apollo_on, http=fake)
    with pytest.raises(apollo.ApolloError, match="--spend-credit"):
        apollo.enrich_contact(db, apollo_on, "p1", job=j, client=client)
    assert fake.calls == [], "no call, no credit, without your confirmation"
    c, what = apollo.enrich_contact(db, apollo_on, "p1", job=j, spend_credit=True, client=client)
    url, payload, _ = fake.calls[0]
    assert url.endswith("/api/v1/people/match") and payload == {"id": "p1", "reveal_personal_emails": False,
                                                                "reveal_phone_number": False}
    assert c.email_status == "apollo_reported" and c.source_type == "apollo_api" and "work email" in what
    with pytest.raises(apollo.ApolloError, match="Daily enrichment limit"):
        apollo.enrich_contact(db, apollo_on, "p2", job=j, spend_credit=True, client=client)
    assert len(fake.calls) == 1
    assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "apollo.enrichment")) == 1


@pytest.mark.parametrize("status,match", [(429, "rate limit"), (401, "rejected the API key"), (500, "HTTP 500")])
def test_api_failures_are_reported_and_not_retried(db, apollo_on, status, match):
    fake = FakeApollo([(status, {"error": "x"})])
    with pytest.raises(apollo.ApolloError, match=match):
        apollo.search_recruiters(db, apollo_on, "examplecorp.com", client=apollo.ApolloClient(apollo_on, http=fake))
    assert len(fake.calls) == 1


def test_personal_email_from_enrichment_is_discarded(db, apollo_on):
    fake = FakeApollo([(200, {"person": {"id": "p9", "name": "Raj T", "email": "raj@gmail.com", "email_status": "verified",
                                         "organization": {"name": "Examplecorp"}}})])
    c, what = apollo.enrich_contact(db, apollo_on, "p9", spend_credit=True, client=apollo.ApolloClient(apollo_on, http=fake))
    assert c.email is None and "personal address" in what


# ---- outreach drafts ---------------------------------------------------------------------------------------------

def test_draft_uses_only_verified_facts_and_the_real_job(profile):
    db = profile
    j = _job(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", title="Technical Recruiter", company_name="Examplecorp",
                                            email="jane@examplecorp.com", email_status="apollo_reported",
                                            source_type="apollo_manual"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=c.id))
    assert d.subject == "Java Backend Engineer role at Examplecorp" and d.to_address == "jane@examplecorp.com"
    assert d.body.startswith("Hi Jane,") and "https://job-boards.greenhouse.io/x/jobs/1" in d.body
    assert "Software Engineer at Synthetic Corp (1.5 years of professional experience)" in d.body
    assert "java, spring boot" in d.body.lower() and "personal project I built LedgerLite" in d.body
    assert "PixelPaint" not in d.body, "only a project that uses a skill this job asks for"
    assert "applied" not in d.body.lower(), "never claims an application that isn't recorded"
    assert "address reported valid by Apollo; JobPilot has not confirmed it" in ds.recipient_warnings(d)
    assert d.job_id == j.id and d.contact_id == c.id and d.status == "draft"


def test_one_outreach_per_recruiter_per_job_and_a_reason_for_a_second_person(profile):
    db = profile
    j = _job(db)
    a, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", company_name="Examplecorp", email="jane@examplecorp.com"))
    b, _ = cs.save_contact(db, cs.ContactIn(name="Raj Talent", company_name="Examplecorp", email="raj@examplecorp.com"))
    ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=a.id))
    with pytest.raises(ds.DraftError, match="already has outreach draft"):
        ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=a.id))
    with pytest.raises(ds.DraftError, match="give the reason"):
        ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=b.id))
    d2 = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=b.id,
                                            additional_contact_reason="Raj hires for the payments team"))
    assert d2.contact_id == b.id
    other_job = _job(db, title="SDE I", url="https://job-boards.greenhouse.io/x/jobs/2")
    assert ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=other_job.id, contact_id=a.id))


def test_suppressed_and_bounced_contacts_get_no_drafts(profile):
    db = profile
    j = _job(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", company_name="Examplecorp", email="jane@examplecorp.com"))
    cs.record_bounce(db, c, "synthetic bounce")
    assert c.email_status == "bounced" and "bounced" in cs.is_blocked(db, "jane@examplecorp.com")
    with pytest.raises(ds.DraftError, match="bounced"):
        ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=c.id))
    d, _ = cs.save_contact(db, cs.ContactIn(name="Opted Out", company_name="Examplecorp", email="no@examplecorp.com"))
    cs.set_dnc(db, d, True, "asked not to be contacted")
    with pytest.raises(ds.DraftError, match="do-not-contact"):
        ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=d.id))


def test_unedited_builtin_template_is_refreshed_but_your_edits_are_kept(db):
    ds.ensure_builtin_templates(db)
    rows = {t.key: t for t in db.scalars(select(EmailTemplate))}
    rows["recruiter_outreach"].body = ds.LEGACY_BUILTIN_BODIES["recruiter_outreach"][0]  # an older, unedited copy
    rows["hiring_manager"].body = "my own text"  # a built-in you edited
    db.flush()
    ds.ensure_builtin_templates(db)
    rows = {t.key: t for t in db.scalars(select(EmailTemplate))}
    assert rows["recruiter_outreach"].body == ds.BUILTIN_TEMPLATES["recruiter_outreach"]["body"]
    assert rows["hiring_manager"].body == "my own text"


# ---- Gmail: drafts never send; bounces are not replies --------------------------------------------------------------

def test_bounce_is_not_a_reply(db, settings):
    from tests.test_m13_gmail import FakeGmail, _draft, client
    from tests.test_m13_gmail import gs as gs_fixture  # noqa: F401
    from jobpilot.integrations import gmail

    settings.gmail_enabled = True
    settings.ensure_dirs()
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    settings.gmail_token_file.write_text(json.dumps({
        "access_token": "at", "refresh_token": "rt", "expires_at": __import__("time").time() + 3600,
        "email": "test.candidate@example.com",
        "scope": "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.metadata"}))
    fake = FakeGmail()
    d = _draft(db, settings)
    gmail.send_via_gmail(db, settings, d, client(settings, fake), "SEND")  # the explicit, typed confirmation path
    fake.threads[d.gmail_thread_id].append({
        "id": "b1", "labelIds": ["INBOX"], "internalDate": str(int((__import__("time").time() + 60) * 1000)),
        "headers": [{"name": "From", "value": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"},
                    {"name": "Subject", "value": "Delivery Status Notification (Failure)"}]})
    res = gmail.sync_replies(db, settings, client(settings, fake))
    assert res["replies"] == 0 and res["bounces"] == 1
    db.refresh(d)
    assert d.reply_received_at is None and d.follow_up_due_at is None and d.contact.email_status == "bounced"
    assert cs.is_blocked(db, d.contact.email)


def test_creating_a_gmail_draft_never_sends(db, settings):
    from tests.test_m13_gmail import FakeGmail, _draft, client
    from jobpilot.integrations import gmail

    settings.gmail_enabled = True
    settings.ensure_dirs()
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    settings.gmail_token_file.write_text(json.dumps({
        "access_token": "at", "refresh_token": "rt", "expires_at": __import__("time").time() + 3600, "email": "me@example.com",
        "scope": "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.metadata"}))
    fake = FakeGmail()
    d = _draft(db, settings)
    for _ in range(2):  # repeated scheduler-like calls: one Gmail draft
        gmail.create_gmail_draft(db, settings, d, client(settings, fake))
    assert fake.count("POST", "/drafts") == 1 and fake.count("POST", "/drafts/send") == 0
    assert d.sent_at is None and d.gmail_status == "created"


# ---- Telegram ------------------------------------------------------------------------------------------------------

@pytest.fixture
def tgs(settings):
    settings.telegram_enabled, settings.telegram_bot_token, settings.telegram_chat_id = True, SecretStr("1:x"), "4242"
    return settings


def _say(db, settings, text, chat=4242):
    upd = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat, "type": "private"}, "text": text}}
    return telegram_bot.handle_update(db, settings, upd)


def test_outreach_commands_report_actual_state(profile, tgs):
    db = profile
    j = _job(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", title="Technical Recruiter", company_name="Examplecorp",
                                            job_id=j.id, email="jane@examplecorp.com", email_status="apollo_reported",
                                            source_type="apollo_manual"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=j.id, contact_id=c.id))
    db.commit()
    rec = _say(db, tgs, "/recruiters")[2]
    assert "Jane Recruiter — Technical Recruiter" in rec and "(apollo_reported)" in rec and f"#{j.id}" in rec
    assert "draft — needs your review" in _say(db, tgs, "/outreach")[2]
    pending = _say(db, tgs, "/outreach_pending")[2]
    assert f"jobpilot outreach approve --draft {d.id}" in pending and "Apollo" in pending
    stats = _say(db, tgs, "/outreach_stats")[2]
    assert "Recruiter contacts: 1" in stats and "Drafts awaiting review: 1" in stats and "Sent: 0" in stats
    assert _say(db, tgs, "/recruiters", chat=999)[0] == "rejected", "contact details only in your private chat"


def test_new_notifications_once_each(profile, tgs):
    db = profile
    from jobpilot.models import ApplicationQuestion
    from jobpilot.services import applications as apps

    j = _job(db)
    cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", company_name="Examplecorp", email="jane@examplecorp.com",
                                     source_type="apollo_manual", email_status="apollo_reported"))
    app_ = apps.create(db, j)
    db.add(ApplicationQuestion(application_id=app_.id, question="What is your current country of residence?",
                               status="unanswered", source="browser"))
    db.commit()
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"daily_summary": False, "questions": True, "recruiter_found": True}})
    notifications.scan(db, tgs)
    db.commit()
    kinds = {n.kind: n.text for n in db.scalars(select(Notification))}
    assert "/answer Q" in kinds["questions"] and "country of residence" in kinds["questions"]
    assert "Jane Recruiter" in kinds["recruiter_found"] and "apollo_reported" in kinds["recruiter_found"]
    assert notifications.scan(db, tgs)["created"] == 0, "nothing announced twice"


# ---- legal attestations and questions the profile has answered --------------------------------------------------

def _app_with_questions(db):
    from jobpilot.models import ApplicationQuestion
    from jobpilot.services import applications as apps

    app_ = apps.create(db, _job(db))
    texts = ["Country", "Location (City)* (no option equals your location)",
             "Please Review the NDA and indicate your agreement by typing your full name below",
             "Will you now or in the future require visa sponsorship?"]
    qs = [ApplicationQuestion(application_id=app_.id, question=t, status="unanswered", source="browser",
                              sensitive=bool(__import__("jobpilot.services.answers", fromlist=["x"]).sensitive_reason(t)))
          for t in texts]
    db.add_all(qs)
    db.commit()
    return app_, qs


def test_legal_questions_are_never_answered_from_telegram(profile, tgs):
    db = profile
    app_, (country, city, nda, visa) = _app_with_questions(db)
    reply = _say(db, tgs, f"/answer Q{nda.id} Test Candidate")[2]
    assert "never answers or signs" in reply
    db.refresh(nda)
    assert nda.status == "unanswered" and nda.answer is None
    assert "answered" in _say(db, tgs, f"/answer Q{visa.id} No")[2], "personal but factual: yours to answer, per job"
    listing = _say(db, tgs, "/questions")[2]
    assert "⚖️ legal (complete it yourself on the form)" in listing and f"Q{nda.id}" not in listing


def test_profile_answered_questions_are_resolved_not_approved(profile):
    from jobpilot.services import answers

    db = profile
    app_, (country, city, nda, visa) = _app_with_questions(db)
    assert answers.unanswered_count(db, app_) == 4
    assert answers.resolve_from_form(db, app_, ["Country*", "Location (City)*", "Please Review the NDA*"]) == 2
    for q in (country, city):
        db.refresh(q)
        assert q.status == "resolved" and q.answer is None and "profile" in q.answer_source
    db.refresh(nda)
    assert nda.status == "unanswered", "a legal item is never closed for you"
    assert answers.unanswered_count(db, app_) == 2


def test_question_alert_routes_legal_items_to_the_form(profile, tgs):
    db = profile
    app_, (country, city, nda, visa) = _app_with_questions(db)
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"daily_summary": False, "questions": True}})
    notifications.scan(db, tgs)
    text = next(n.text for n in db.scalars(select(Notification)) if n.kind == "questions")
    assert "⚖️ legal — complete it yourself on the form, not here" in text and f"Q{visa.id} 🔒 personal" in text


def test_an_approved_answer_is_never_typed_into_a_legal_field(world, db, chrome):  # noqa: F811
    """Even if an answer for the NDA question exists, the signature field stays empty."""
    from jobpilot.automation.apply import assist_page
    from tests.test_m28_form_filling import form, text, open_page as _op  # noqa: F401

    sync = pytest.importorskip("playwright.sync_api")
    from jobpilot.config import get_settings
    from tests.helpers import LocalPage

    html = form(text("nda", "Please Review the NDA and indicate your agreement by typing your full name below"),
                "<fieldset><legend>I agree to the terms*</legend><label><input type=radio name=t value=Yes required> Yes"
                "</label><label><input type=radio name=t value=No required> No</label></fieldset>")
    site = LocalPage(html)
    try:
        with sync.sync_playwright() as pw:
            b = pw.chromium.launch(channel=get_settings().browser_channel or None, headless=True)
            page = b.new_page()
            page.goto(site.url)
            res = assist_page(page, {}, None, answer_for=lambda label, sensitive: "Test Candidate" if "NDA" in label else "Yes")
            assert page.input_value("#nda") == "" and not page.is_checked("input[name=t][value=Yes]")
            legal = {e["field"] for e in res.report["legal_decision"]}
            assert {"Please Review the NDA and indicate your agreement by typing your full name below*",
                    "I agree to the terms*"} <= legal
            b.close()
    finally:
        site.close()


from tests.apply_fixtures import chrome, world  # noqa: E402,F401
