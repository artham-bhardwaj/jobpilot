"""Milestone 37: the four agents (Search, Resume, Apply, Manager). Synthetic data only: fake HTTP for feeds and
board discovery, a fake Gmail, and a fake employer form on 127.0.0.1 for real browser submissions. No real employer,
board or mailbox is contacted."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from jobpilot.agents import apply as apply_agent
from jobpilot.agents import manager, run_cycle
from jobpilot.agents.base import applied_today, last_report, set_paused
from jobpilot.models import Application, AuditLog, BrowserRun, EmailDraft, JobSource, ReviewItem
from jobpilot.services import answers as answer_svc
from jobpilot.services import board_discovery, job_alerts, settings_store, wording
from jobpilot.sources.feeds import RemoteOKAdapter, WeWorkRemotelyAdapter
from tests.apply_fixtures import EmployerSite, job, world  # noqa: F401


def _policy(db, **kw):
    pol = {**settings_store.get(db, "agents"), "add_feed_sources": False, "job_alert_emails": False,
           "discover_boards": False, **kw}
    settings_store.put(db, "agents", pol)
    db.commit()
    return pol


def _no_search(monkeypatch):
    from jobpilot.services import jobs as job_svc

    monkeypatch.setattr(job_svc, "collect_all", lambda *a, **k: {"sources": {}, "new": 0, "failed": 0})


# ---- Resume agent: rewording ---------------------------------------------------------------------------------

def test_rewording_uses_the_postings_spelling_of_the_same_skill_only():
    swaps = wording.plan("Built APIs on Postgres and K8s with React. Java 17.",
                         "We use PostgreSQL, Kubernetes and React.js. Java required.")
    assert swaps == {"Postgres": "PostgreSQL", "K8s": "Kubernetes", "React": "React.js"}
    assert wording.plan("Java and Docker", "Java 17, EKS") == {}, "versions and narrower terms are never swaps"
    tex, done = wording.apply(r"Postgres at \href{https://x.io/postgres}{postgres-tool} and Postgres", {"Postgres": "PostgreSQL"})
    assert tex == r"PostgreSQL at \href{https://x.io/postgres}{postgres-tool} and PostgreSQL"
    assert done == [("Postgres", "PostgreSQL"), ("Postgres", "PostgreSQL")]
    assert wording.plan("REST APIs", "Design RESTful APIs") == {"REST APIs": "RESTful APIs"}


# ---- Search agent ------------------------------------------------------------------------------------------------

class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, url, params=None):
        from jobpilot.sources.http import FetchError

        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(f"HTTP 404 for {url}", status=404)
        return self.routes[url]


def test_feeds_keep_only_wanted_titles_and_regions():
    http = FakeHttp({RemoteOKAdapter.URL: [
        {"legal": "credit Remote OK"},
        {"id": "1", "position": "Backend Engineer", "company": "Acme", "location": "Worldwide",
         "url": "https://remoteok.com/remote-jobs/1", "date": "2026-10-20T10:00:00+00:00", "description": "<p>Java</p>"},
        {"id": "2", "position": "Backend Engineer", "company": "Beta", "location": "USA Only",
         "url": "https://remoteok.com/remote-jobs/2"},
        {"id": "3", "position": "Senior Backend Engineer", "company": "Gamma", "url": "https://remoteok.com/remote-jobs/3"},
    ]})
    res = RemoteOKAdapter().fetch(http, {"title_keywords": ["backend"], "exclude_keywords": ["senior"]}, None)
    assert [j.company_name for j in res.items] == ["Acme"], "USA-only and senior roles are dropped"
    rss = """<rss><channel><item><title>Acme: Software Engineer</title><region>Anywhere in the World</region>
      <pubDate>Tue, 06 Oct 2026 07:31:05 +0000</pubDate><guid>https://weworkremotely.com/remote-jobs/acme-se</guid>
      <description>&lt;p&gt;Python&lt;/p&gt;</description></item></channel></rss>"""
    item = WeWorkRemotelyAdapter.parse(rss)[0]
    assert item["title"] == "Acme: Software Engineer" and item["guid"].endswith("acme-se")


def test_board_discovery_accepts_only_the_companys_own_board():
    http = FakeHttp({board_discovery.GREENHOUSE.format("acmelabs"): {"name": "Acme Labs Job Board"},
                     board_discovery.GREENHOUSE.format("other"): {"name": "Someone Else"},
                     board_discovery.LEVER.format("betaco"): [{"id": "x"}]})
    assert board_discovery.probe(http, "Acme Labs Inc.") == {"kind": "greenhouse", "token": "acmelabs",
                                                             "config_key": "board_token"}
    assert board_discovery.probe(http, "BetaCo") == {"kind": "lever", "token": "betaco", "config_key": "site"}
    assert board_discovery.probe(http, "Other") is None, "a board named after someone else is not accepted"


def test_board_discovery_adds_sources_for_aggregator_companies(world, db):  # noqa: F811
    from jobpilot.services import jobs as job_svc

    src = job_svc.get_or_create_source(db, "Remote OK (remote)", "manual")
    j = job(db, "https://remoteok.com/remote-jobs/9", company="Acme Labs")
    from jobpilot.models import JobSighting

    db.add(JobSighting(job_id=j.id, source_id=src.id, method="api"))
    db.commit()
    src.kind = "remoteok"
    db.commit()
    http = FakeHttp({board_discovery.GREENHOUSE.format("acmelabs"): {"name": "Acme Labs"}})
    out = board_discovery.run(db, http, 5)
    assert out["added"] == ["Acme Labs (Greenhouse)"]
    s = db.scalar(select(JobSource).where(JobSource.name == "Acme Labs (Greenhouse)"))
    assert s.kind == "greenhouse" and s.config["board_token"] == "acmelabs" and "remote" in s.config["regions"]
    assert board_discovery.run(db, http, 5)["checked"] == [], "checked once per 30 days"


def test_job_alert_emails_are_parsed_from_the_lines_above_each_link():
    text = """Your job alert for software engineer

Software Engineer II
Acme Corp
Bengaluru, Karnataka, India
View job: https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc

Promoted
Backend Developer
Beta Ltd
Remote
https://www.linkedin.com/comm/jobs/view/4099999999/?refId=x
Unsubscribe: https://www.linkedin.com/comm/psettings/email"""
    items = job_alerts.parse(text)
    assert [(i["title"], i["company"], i["location"], i["url"]) for i in items] == [
        ("Software Engineer II", "Acme Corp", "Bengaluru, Karnataka, India", "https://www.linkedin.com/jobs/view/4012345678/"),
        ("Backend Developer", "Beta Ltd", "Remote", "https://www.linkedin.com/jobs/view/4099999999/")]


# ---- Apply agent: email applications -----------------------------------------------------------------------------

def test_application_email_is_found_only_when_the_posting_asks_for_it():
    from jobpilot.models import Job

    j = Job(title="SE", description_text="About us.\nInterested candidates can send their CV to hr@acme.example.org today.")
    assert apply_agent.application_email(j) == ("hr@acme.example.org", "Interested candidates can send their CV to "
                                                                          "hr@acme.example.org today.")
    j2 = Job(title="SE", description_text="Questions about privacy? Email privacy@acme.io. Apply on our site.")
    assert apply_agent.application_email(j2) is None
    j3 = Job(title="SE", description_text="Please email your resume to privacy@acme.io")
    assert apply_agent.application_email(j3) is None, "privacy / legal inboxes are never application addresses"


def test_email_application_sent_in_auto_mode_and_never_twice(world, db, monkeypatch):  # noqa: F811
    from jobpilot.integrations import gmail
    from tests.test_m13_gmail import FakeGmail

    world.gmail_enabled = True
    world.ensure_dirs()
    world.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    world.gmail_token_file.write_text(json.dumps({"access_token": "at", "refresh_token": "rt", "expires_at": 9e12,
                                                  "scope": gmail.SCOPE_COMPOSE + " " + gmail.SCOPE_METADATA}))
    _no_search(monkeypatch)
    _policy(db, apply_mode="auto")
    j = job(db, "https://acme.example.org/careers/1", company="Acme",
            description_text="Java backend role.\nSend your resume to careers@acme-hiring.com with the subject line.")
    fake = FakeGmail()
    client = gmail.GmailClient(world, http=fake)
    res = run_cycle(db, world, gmail_client=client)
    app = db.scalar(select(Application).where(Application.job_id == j.id))
    assert app.applied_at is not None and app.confirmation_method == "email_sent", res
    d = db.scalar(select(EmailDraft).where(EmailDraft.job_id == j.id))
    assert d.kind == "application" and d.status == "sent" and d.to_address == "careers@acme-hiring.com"
    assert "Test Candidate" in d.body or "Candidate" in d.body
    assert applied_today(db) == 1
    run_cycle(db, world, gmail_client=client)
    assert db.scalar(select(EmailDraft.id).where(EmailDraft.job_id == j.id, EmailDraft.id != d.id)) is None
    sends = [c for c in fake.calls if c[1].endswith("/drafts/send")]
    assert len(sends) == 1, "one application email per job, ever"


def test_dry_run_sends_nothing(world, db, monkeypatch):  # noqa: F811
    _no_search(monkeypatch)
    _policy(db)  # default apply_mode is dry_run
    assert settings_store.get(db, "agents")["apply_mode"] == "dry_run"
    j = job(db, "https://acme.example.org/careers/2", company="Acme",
            description_text="Send your CV to jobs@acme-hiring.com")
    res = run_cycle(db, world)
    app = db.scalar(select(Application).where(Application.job_id == j.id))
    assert app.applied_at is None
    d = db.scalar(select(EmailDraft).where(EmailDraft.job_id == j.id))
    assert d.status == "draft" and d.gmail_status is None
    assert "would_email" in res["reports"]["apply"]
    assert manager.select_jobs(db, settings_store.get(db, "agents"), "dry_run")["picked"] == [], \
        "a job checked in a dry run isn't repeated for 24 h"


# ---- Apply agent: forms (real headless browser against a local fake employer) ------------------------------------

@pytest.fixture
def browser_ok():
    pytest.importorskip("playwright.sync_api")


def _bank(db):
    answer_svc.save_entry(db, "Years of Java experience", "1-3", category="experience", approved=True)
    db.commit()


def test_form_submitted_when_safe_and_recorded(world, db, monkeypatch, browser_ok):  # noqa: F811
    _no_search(monkeypatch)
    _bank(db)
    _policy(db, apply_mode="auto", email_applications=False)
    site = EmployerSite("confirm")
    try:
        j = job(db, site.url)
        run_cycle(db, world)
    finally:
        site.close()
    app = db.scalar(select(Application).where(Application.job_id == j.id))
    assert site.posts == 1 and app.applied_at is not None and app.confirmation_method == "confirmation_page"
    run = db.scalar(select(BrowserRun).where(BrowserRun.application_id == app.id))
    assert run.status == "confirmed"
    assert db.scalar(select(AuditLog.id).where(AuditLog.action == "agents.form_submitted")) is not None


def test_captcha_and_unanswered_questions_come_to_you_unsubmitted(world, db, monkeypatch, browser_ok):  # noqa: F811
    _no_search(monkeypatch)
    _policy(db, apply_mode="auto", email_applications=False)
    captcha, plain = EmployerSite("captcha"), EmployerSite("confirm")
    try:
        a = job(db, captcha.url, title="Backend Engineer A")
        b = job(db, plain.url, title="Backend Engineer B")  # "Years of Java experience" has no approved answer
        run_cycle(db, world)
    finally:
        captcha.close()
        plain.close()
    assert captcha.posts == 0 and plain.posts == 0, "nothing is submitted past a check or with a missing answer"
    items = {i.entity_id: i for i in db.scalars(select(ReviewItem).where(ReviewItem.kind == apply_agent.NEEDS_YOU))}
    app_a = db.scalar(select(Application).where(Application.job_id == a.id))
    app_b = db.scalar(select(Application).where(Application.job_id == b.id))
    assert "jobpilot assist" in items[app_a.id].reason and "CAPTCHA" in items[app_a.id].reason
    assert "Years of Java experience" in items[app_b.id].reason
    # You answer the question; the next cycle continues on its own.
    from jobpilot.models import ApplicationQuestion

    for q in db.scalars(select(ApplicationQuestion).where(ApplicationQuestion.application_id == app_b.id)):
        q.answer, q.status = "1-3", "approved"
    db.commit()
    picked = manager.select_jobs(db, settings_store.get(db, "agents"), "auto")["picked"]
    assert [j.id for j in picked] == [b.id]


def test_uncertain_submission_is_never_repeated(world, db, monkeypatch):  # noqa: F811
    _no_search(monkeypatch)
    _bank(db)
    _policy(db, apply_mode="auto", email_applications=False)
    j = job(db, "https://boards.greenhouse.io/acme/jobs/1")
    calls = []

    def runner(settings, url, values, resume_path, answer_for, before_click, headless=True):
        calls.append(url)
        assert before_click()
        return {"stage": "submitted", "outcome": "uncertain", "detail": "no answer within 30 s", "filled": []}

    run_cycle(db, world, runner=runner)
    run_cycle(db, world, runner=runner)
    assert len(calls) == 1
    app = db.scalar(select(Application).where(Application.job_id == j.id))
    assert app.applied_at is None and app.browser_runs[0].status == "uncertain"
    assert applied_today(db) == 1, "an unclear attempt still counts against the daily limit"


def test_daily_limit_pause_and_dream_companies(world, db, monkeypatch):  # noqa: F811
    _no_search(monkeypatch)
    _policy(db, apply_mode="auto", email_applications=False, daily_limit=1)
    calls = []

    def runner(settings, url, values, resume_path, answer_for, before_click, headless=True):
        calls.append(url)
        assert before_click()
        return {"stage": "submitted", "outcome": "confirmed", "detail": "ok", "url": url, "text": "Thank you", "filled": []}

    job(db, "https://boards.greenhouse.io/acme/jobs/1", title="Backend Engineer 1", score=90)
    job(db, "https://boards.greenhouse.io/acme/jobs/2", title="Backend Engineer 2", score=85)
    g = job(db, "https://www.google.com/about/careers/x", title="Backend Engineer 3", company="Google", score=95)
    set_paused(db, True, "test")
    db.commit()
    run_cycle(db, world, runner=runner)
    assert calls == [], "paused: nothing submitted"
    set_paused(db, False, "test")
    db.commit()
    run_cycle(db, world, runner=runner)
    run_cycle(db, world, runner=runner)
    assert len(calls) == 1, "daily limit of 1"
    held = db.scalar(select(Application).where(Application.job_id == g.id))
    item = apply_agent.open_item(db, held.id)
    assert item is not None and (item.data or {}).get("code") == "dream_company"
    assert any("Dream company" in i["detail"] for r in [last_report(db, "apply")] for i in r["items"]) or \
        "Dream company" in item.reason


# ---- Manager, CLI-free controls and the page ---------------------------------------------------------------------

def test_agents_page_and_controls(world, client, db, monkeypatch):  # noqa: F811
    _no_search(monkeypatch)
    _policy(db)
    job(db, "https://www.google.com/about/careers/x", company="Google")  # held for you: a "Needs you" row
    run_cycle(db, world)
    page = client.get("/agents")
    assert "Dream company" in page.text and "Resume agent" in page.text
    assert page.status_code == 200 and "Needs you" in page.text and "Dry run" in page.text
    r = client.post("/agents/mode", data={"mode": "auto"})
    assert "err=" in r.headers["location"], "auto needs the confirmation tick"
    client.post("/agents/mode", data={"mode": "auto", "confirm": "1"})
    assert settings_store.get(db, "agents")["apply_mode"] == "auto"
    client.post("/agents/schedule", data={"enabled": "1", "every_hours": "4"})
    assert settings_store.get(db, "scheduler")["tasks"]["agents"] == {"enabled": True, "every_hours": 4.0}
    client.post("/agents/pause", data={"action": "pause"})
    assert "Paused" in client.get("/agents").text
    client.post("/agents/policy", data={"min_score": "65", "daily_limit": "3", "max_per_cycle": "2",
                                        "allowed_eligibility": ["eligible"], "email_applications": "1"})
    pol = settings_store.get(db, "agents")
    assert pol["min_score"] == 65 and pol["daily_limit"] == 3 and pol["allowed_eligibility"] == ["eligible"]
    assert pol["email_applications"] is True and pol["form_submissions"] is False
    assert 'href="/agents"' in client.get("/").text


# ---- Resume agent: a reworded resume compiles and passes every check ------------------------------------------

from tests.test_m34_tailored import needs_tex, tworld  # noqa: E402,F401


@needs_tex
def test_reworded_resume_passes_validation(tworld, db):  # noqa: F811
    from jobpilot.services import resumes as resume_svc
    from jobpilot.services import tailored

    s, j = tworld.settings, tworld.job
    j.description_text = (j.description_text or "") + "\nYou will design RESTful APIs."
    db.commit()
    tr = tailored.generate(db, s, j, force=True)
    db.commit()
    assert tr.status == "valid", tr.error
    assert tr.decisions["rewording"] == [{"from": "REST APIs", "to": "RESTful APIs", "count": 1}]
    text = resume_svc.extract_text("x.pdf", (s.files_dir / tr.pdf_path).read_bytes())["text"]
    assert "RESTful APIs" in text and all(c["ok"] for c in tr.validation["checks"].values())

    settings_store.put(db, "tailored_resumes", {**settings_store.get(db, "tailored_resumes"), "reword_skills": False})
    db.commit()
    plain = tailored.generate(db, s, j, force=True)
    assert plain.decisions["rewording"] == [] and plain.status == "valid"
