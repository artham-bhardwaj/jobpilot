"""Stage 5: interviews, invitation extraction, prep packs, skill gaps, analytics/CSV and Gmail (mocked)."""

from __future__ import annotations

import base64
import csv
import io
import json
import time
from datetime import datetime

import pytest
import requests
from sqlalchemy import select

from jobpilot.models import EmailDraft, Interview, ReviewItem, Skill
from jobpilot.services import analytics, applications as apps, contacts as cs, drafts as ds
from jobpilot.services import interviews as iv_svc, skill_gaps
from tests.test_stage4 import _ready, _setup

INVITE = """Subject: Interview invitation - Examplecorp
Hi Test,
Thanks for applying to Examplecorp. We'd like to invite you to a technical interview (coding round)
on Tuesday, October 21, 2026 at 3:30 PM IST.
Join: https://meet.google.com/abc-defg-hij
Regards, Recruiting (synthetic)"""


def _applied(db, settings):
    job, v = _setup(db, settings)
    app = apps.create(db, job)
    _ready(db, app)
    apps.approve_submission(db, app)
    apps.add_evidence(db, settings, app, "confirmation_email", "Thanks for applying (synthetic)")
    db.refresh(app)
    apps.record_submission(db, app, "confirmation_email", "", attest=True)
    return job, app


# ---- Interviews -------------------------------------------------------------------

def test_extract_invitation():
    s = iv_svc.extract_invitation(INVITE)
    assert s["scheduled_at"] == "2026-10-21T15:30:00" and s["time_known"]
    assert s["timezone"] == "IST" and s["format"] == "video" and s["stage"] == "coding"
    assert s["location_or_link"] == "https://meet.google.com/abc-defg-hij"
    oa = iv_svc.extract_invitation("Please complete the online assessment by 25 Oct 2026: https://www.hackerrank.com/test/xyz")
    assert oa["stage"] == "online_assessment" and oa["format"] == "online_test"
    assert iv_svc.extract_invitation("Our newsletter for October") is None


def test_suggestion_never_creates_interview(db, settings):
    job, app = _applied(db, settings)
    iv_svc.suggest_from_text(db, app, INVITE, "pasted")
    assert db.scalar(select(Interview)) is None
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "interview_detected"))
    assert item.status == "open" and item.data["location_or_link"]


def test_interview_lifecycle_and_prep(db, settings):
    job, app = _applied(db, settings)
    iv = iv_svc.save(db, iv_svc.InterviewIn(application_id=app.id, stage="technical", scheduled_at=datetime(2026, 10, 21, 15, 30),
                                            format="video", location_or_link="https://meet.google.com/abc"))
    assert iv.round_no == 1 and app.status == "interview"
    with pytest.raises(Exception):
        iv_svc.InterviewIn(application_id=app.id, location_or_link="javascript:alert(1)")
    pack = iv_svc.generate_prep_pack(db, iv)
    titles = [s["title"] for s in pack.content["sections"]]
    assert "Data structures & algorithms" in titles and "Java" in titles and "Spring Boot" in titles and "SQL & databases" in titles
    assert "java" in pack.content["job_skills_you_have"]
    assert any("Synthetic Corp" in s for s in pack.content["stories"])
    assert pack.content["header"]["resume"]
    sd = iv_svc.save(db, iv_svc.InterviewIn(application_id=app.id, stage="system_design"))
    titles = [s["title"] for s in iv_svc.generate_prep_pack(db, sd).content["sections"]]
    assert titles[0].startswith("High-level") and "Low-level design" in titles
    iv_svc.record_outcome(db, iv, iv_svc.OutcomeIn(status="completed", outcome="passed", follow_up_actions="send thanks"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="thank_you", interview_id=iv.id))
    assert "21 Oct 2026" in d.body and "Java Backend Engineer" in d.body


# ---- Skill gaps ---------------------------------------------------------------------

def test_skill_gaps_and_mastery_requires_confirmation(db, settings):
    from jobpilot.services import jobs as job_svc
    from tests.helpers import seed_profile

    seed_profile(db, skills=("java",), learning=())
    for i in range(3):
        job_svc.add_manual(db, job_svc.ManualJobIn(title=f"Java Backend Engineer {i}", company_name=f"Co{i}",
                                                   description_text="Requirements: Java, Kafka, Docker. 1+ years of experience"))
    r = skill_gaps.analyze(db, scope="all")
    gaps = {g["skill"]: g for g in r["gaps"]}
    assert gaps["kafka"]["mandatory"] == 3 and "java" not in gaps
    item = skill_gaps.add_to_backlog(db, "kafka", gaps["kafka"]["priority"])
    assert db.scalar(select(Skill).where(Skill.normalized_name == "kafka")).status == "learning"
    skill_gaps.update(db, item, skill_gaps.LearningUpdate(status="ready_for_confirmation", progress_pct=100))
    assert item.progress_pct == 95, "self-reported progress cannot reach 100%"
    assert db.scalar(select(Skill).where(Skill.normalized_name == "kafka")).status == "learning"
    with pytest.raises(ValueError):
        skill_gaps.confirm_mastery(db, item, "yes")
    skill_gaps.confirm_mastery(db, item, "Built an event pipeline project with Kafka (synthetic)")
    assert db.scalar(select(Skill).where(Skill.normalized_name == "kafka")).status == "confirmed"


# ---- Analytics ------------------------------------------------------------------------

def test_analytics_insufficient_data_and_csv(db, settings):
    s = analytics.summary(db)
    assert s["applications"]["response_rate"]["value"] is None and s["applications"]["response_rate"]["note"] == "no data"
    job, app = _applied(db, settings)
    apps.transition(db, app, "rejected")
    s = analytics.summary(db)
    rr = s["applications"]["response_rate"]
    assert rr["k"] == 1 and rr["n"] == 1 and rr["value"] is None and "insufficient" in rr["note"]
    assert s["jobs"]["total"] == 1 and s["resumes"][0]["applications"] == 1
    for kind in analytics.EXPORT_KINDS:
        rows = list(csv.reader(io.StringIO(analytics.export_csv(db, kind))))
        assert rows[0], kind
    job.title = "=HYPERLINK(\"http://evil\")"
    out = analytics.export_csv(db, "jobs")
    assert "'=HYPERLINK" in out, "formula injection must be neutralised"


def test_rates_with_enough_data():
    r = analytics.Rate(2, 8)
    assert r.value == 0.25 and r.note == "2/8"


# ---- Gmail (offline: Google endpoints are mocked) ----------------------------------------

class GoogleStub(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = requests.Response()
        r.status_code = 200
        body: dict = {}
        if url.endswith("/token"):
            body = {"access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-1", "scope": "compose metadata"}
        elif url.endswith("/profile"):
            body = {"emailAddress": "test.candidate@example.com"}
        elif url.endswith("/drafts") and method == "POST":
            body = {"id": "d-1", "message": {"id": "m-0", "threadId": "t-1"}}
        elif url.endswith("/drafts/send"):
            body = {"id": "m-1", "threadId": "t-1"}
        elif "/threads/" in url:
            body = {"messages": [
                {"internalDate": str(int(time.time() * 1000) - 1000),
                 "payload": {"headers": [{"name": "From", "value": "Test <test.candidate@example.com>"}]}},
                {"internalDate": str(int(time.time() * 1000) + 60_000),
                 "payload": {"headers": [{"name": "From", "value": "Jane <jane@example.com>"}]}}]}
        r._content = json.dumps(body).encode()
        return r

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


@pytest.fixture
def gmail_settings(settings):
    settings.gmail_enabled = True
    settings.ensure_dirs()
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com",
                                                                     "client_secret": "secret"}}))
    return settings


def test_gmail_auth_url_and_token_storage(gmail_settings):
    from jobpilot.integrations import gmail

    url = gmail.build_auth_url(gmail_settings, "http://127.0.0.1:5555/", "st", "v" * 64)
    assert "code_challenge_method=S256" in url and "gmail.compose" in url and "gmail.metadata" in url
    assert "gmail.readonly" not in url and "gmail.send" not in url
    tok = gmail.exchange_code(gmail_settings, "code", "http://127.0.0.1:5555/", "v" * 64, http=GoogleStub())
    assert tok["refresh_token"] == "rt-1"
    assert oct(gmail_settings.gmail_token_file.stat().st_mode)[-3:] == "600"
    assert gmail.is_ready(gmail_settings)


def test_gmail_send_requires_approval_and_confirmation(db, gmail_settings):
    from jobpilot.integrations import gmail
    from jobpilot.services import jobs as job_svc
    from tests.helpers import seed_profile

    seed_profile(db)
    gmail.exchange_code(gmail_settings, "code", "http://127.0.0.1:1/", "v" * 64, http=GoogleStub())
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", company_name="Examplecorp",
                                                        description_text="Requirements: Java"))
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com", company_name="Examplecorp"))
    cs.verify_email(db, c, "synthetic test: address confirmed")  # sending requires a verified recipient
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", job_id=job.id, contact_id=c.id))
    stub = GoogleStub()
    client = gmail.GmailClient(gmail_settings, http=stub)
    with pytest.raises(gmail.GmailError):
        gmail.send_via_gmail(db, gmail_settings, d, client, "SEND")  # not approved
    ds.approve(db, d)
    with pytest.raises(gmail.GmailError):
        gmail.send_via_gmail(db, gmail_settings, d, client, "yes")  # no explicit confirmation
    gmail.send_via_gmail(db, gmail_settings, d, client, "SEND")
    assert d.status == "sent" and d.sent_via == "gmail" and d.gmail_thread_id == "t-1"
    raw = next(kw["json"]["message"]["raw"] for m, u, kw in stub.calls if u.endswith("/drafts"))
    mime = base64.urlsafe_b64decode(raw + "==").decode(errors="replace")
    assert "To: jane@example.com" in mime and "Subject:" in mime
    res = gmail.sync_replies(db, gmail_settings, client)
    assert res["replies"] == 1 and d.reply_received_at and d.follow_up_due_at is None


def test_gmail_daily_limit_and_dnc(db, gmail_settings):
    from jobpilot.integrations import gmail
    from jobpilot.services import settings_store
    from tests.helpers import seed_profile

    seed_profile(db)
    gmail.exchange_code(gmail_settings, "code", "http://127.0.0.1:1/", "v" * 64, http=GoogleStub())
    settings_store.put(db, "outreach", {"follow_up_days": 7, "max_follow_ups": 2, "daily_send_limit": 1})
    client = gmail.GmailClient(gmail_settings, http=GoogleStub())
    drafts = []
    for i in range(2):
        c, _ = cs.save_contact(db, cs.ContactIn(name=f"Person {i}", email=f"p{i}@example.org", company_name="Examplecorp"))
        cs.verify_email(db, c, "synthetic test: address confirmed")  # sending requires a verified recipient
        d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", contact_id=c.id))
        ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject="Hello", body="Hi, synthetic test."))
        ds.approve(db, d)
        drafts.append(d)
    gmail.send_via_gmail(db, gmail_settings, drafts[0], client, "SEND")
    with pytest.raises(gmail.GmailError, match="limit"):
        gmail.send_via_gmail(db, gmail_settings, drafts[1], client, "SEND")
    cs.add_dnc(db, cs.DncIn(kind="email", value="p1@example.org"))
    db.refresh(drafts[1])
    with pytest.raises(gmail.GmailError):
        gmail.create_gmail_draft(db, gmail_settings, drafts[1], client)


def test_gmail_disabled_by_default(settings):
    from jobpilot.integrations import gmail

    assert not gmail.is_ready(settings)
    with pytest.raises(gmail.GmailError):
        gmail.GmailClient(settings)


# ---- Web ----------------------------------------------------------------------------------

def test_stage5_pages(client, db, settings):
    job, app = _applied(db, settings)
    db.commit()
    for url in ("/interviews", "/skills", "/skills?scope=all", "/analytics", "/gmail", f"/applications/{app.id}"):
        assert client.get(url).status_code == 200, url
    r = client.post(f"/applications/{app.id}/invitation", data={"text": INVITE})
    assert r.status_code == 303 and "suggestion=" in r.headers["location"]
    page = client.get(r.headers["location"].split("&msg")[0].split("?msg")[0]).text
    assert "meet.google.com" in page and "Confirm suggested interview" in page
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "interview_detected"))
    r = client.post("/interviews", data={"application_id": app.id, "stage": "coding", "format": "video",
                                         "scheduled_at": "2026-10-21T15:30", "suggestion_id": str(item.id)})
    assert r.status_code == 303 and "/interviews/" in r.headers["location"]
    iv = db.scalar(select(Interview))
    assert iv.source == "email"
    assert client.post(f"/interviews/{iv.id}/prep").status_code == 303
    assert "Preparation pack" in client.get(f"/interviews/{iv.id}").text
    r = client.get("/analytics/export/applications.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Java Backend Engineer" in r.text
    assert client.post("/skills/backlog", data={"skill": "kafka"}).status_code == 303
