"""Milestone 33: your application tracker — did you apply (and with which resume), applications by company with
cooldowns, dream companies first, and employer emails (headers only) updating the status. Synthetic data, fake
Telegram updates (handler level) and a fake Gmail; nothing is sent anywhere."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from jobpilot.models import Application, ApplicationEvent, Notification, ResumeVersion, ReviewItem, utcnow
from jobpilot.services import applications as apps
from jobpilot.services import notifications, settings_store, telegram_bot, telegram_ui, tracker
from tests.apply_fixtures import job, tg, world  # noqa: F401
from tests.test_m27_automation import InboxGmail, gmail_ready  # noqa: F401


def _press(data, user="4242"):
    return {"id": "cb", "from": {"id": int(user)}, "data": data, "message": {"chat": {"id": 4242, "type": "private"}}}


def _cb(db, settings, data, user="4242"):
    out = telegram_bot.handle_callback(db, settings, _press(data, user))
    db.commit()
    return out


def _say(db, settings, text):
    out = telegram_bot.handle_update(db, settings, {"update_id": 1, "message": {
        "message_id": 1, "chat": {"id": 4242, "type": "private"}, "text": text}})
    db.commit()
    return out


def _version(db):
    return db.scalar(select(ResumeVersion).where(ResumeVersion.status == "approved"))


def test_dream_companies_are_faang_by_default_and_listed_first(world, db):  # noqa: F811
    assert set(settings_store.DEFAULTS["tracker"]["dream_companies"]) >= {"Meta", "Apple", "Amazon", "Netflix", "Google"}
    assert settings_store.DEFAULTS["tracker"]["cooldown_days"] == {}, "no cooldown is ever assumed"
    assert tracker.is_dream(db, "Amazon Web Services") and tracker.is_dream(db, "Google LLC")
    assert not tracker.is_dream(db, "Amazonia Logistics") and not tracker.is_dream(db, "Examplecorp")
    job(db, "https://example.com/1", company="Examplecorp", score=95)
    job(db, "https://example.com/2", title="SDE I", company="Amazon", score=60)
    job(db, "https://example.com/3", title="Staff Engineer", company="Google", score=90, eligibility="ineligible")
    names = [j.company_name for j in tracker.relevant_jobs(db)]
    assert names == ["Amazon", "Examplecorp"], "dream first; ineligible left out"
    assert len(tracker.relevant_jobs(db, include_ineligible=True)) == 3


def test_telegram_did_you_apply_then_which_resume(tg, db):  # noqa: F811
    j = job(db, "https://www.amazon.jobs/en/jobs/1", title="SDE I", company="Amazon")
    listing = _say(db, tg, "/track")[2]
    assert "Did you apply" in listing and "https://www.amazon.jobs/en/jobs/1" in listing and "⭐" in listing
    kb = str(telegram_ui.command_markup(db, "/track"))
    assert f"t:a:{j.id}" in kb and f"t:n:{j.id}" in kb and f"t:x:{j.id}" in kb and "amazon.jobs" in kb

    assert _cb(db, tg, f"t:a:{j.id}", user="999")[0] == "rejected", "only you"
    res, command, reply = _cb(db, tg, f"t:a:{j.id}")
    assert command == f"t:a:{j.id}" and "Which resume" in reply
    v = _version(db)
    picker = telegram_bot._reply_markup(db, command)
    datas = [b["callback_data"] for r in picker["inline_keyboard"] for b in r]
    assert datas == [f"t:r:{j.id}:{v.id}", f"t:r:{j.id}:0"]
    assert db.scalar(select(Application)) is None, "nothing recorded until you pick the resume"

    reply = _cb(db, tg, f"t:r:{j.id}:{v.id}")[2]
    assert reply.startswith("✅ Recorded") and "Master resume v1" in reply
    app = apps.for_job(db, j.id)
    assert app.status == "applied" and app.applied_at and app.confirmation_method == "self_reported"
    assert app.resume_version_id == v.id
    assert "Already recorded" in _cb(db, tg, f"t:a:{j.id}")[2]
    assert "Already recorded" in _cb(db, tg, f"t:r:{j.id}:{v.id}")[2] or "Not recorded" in _cb(db, tg, f"t:r:{j.id}:0")[2]
    assert db.query(Application).count() == 1, "never twice"
    assert tracker.to_ask(db) == [], "not asked again"


def test_another_resume_and_unapproved_versions(tg, db):  # noqa: F811
    j = job(db, "https://example.com/1")
    assert "Not recorded" in _cb(db, tg, f"t:r:{j.id}:99999")[2]
    assert apps.for_job(db, j.id) is None
    _cb(db, tg, f"t:r:{j.id}:0")
    app = apps.for_job(db, j.id)
    assert app.applied_at and app.resume_version_id is None and tracker.resume_label(app).startswith("another resume")


def test_not_yet_asks_again_later_and_not_interested_dismisses(tg, db):  # noqa: F811
    later, nope = job(db, "https://example.com/1"), job(db, "https://example.com/2", title="Other")
    assert "ask again" in _cb(db, tg, f"t:n:{later.id}")[2]
    assert "Dismissed" in _cb(db, tg, f"t:x:{nope.id}")[2]
    db.refresh(nope)
    assert nope.user_status == "dismissed" and tracker.to_ask(db) == []
    checks = notifications.get_state(db, tracker.CHECKS_KEY)
    checks[str(later.id)]["at"] = (utcnow() - timedelta(days=4)).isoformat()
    notifications.set_state(db, tracker.CHECKS_KEY, checks)
    assert [j.id for j in tracker.to_ask(db)] == [later.id], "asked again after ask_again_days"


def test_applications_by_company_and_cooldown(tg, db):  # noqa: F811
    a1 = job(db, "https://example.com/1", title="SDE I", company="Amazon")
    a2 = job(db, "https://example.com/2", title="SDE I, AWS", company="Amazon Web Services")
    tracker.record_self_applied(db, a1, None, applied_on=(utcnow() - timedelta(days=10)).date())
    db.commit()
    assert "No cooldown set" in _say(db, tg, "/company amazon")[2]
    assert "Cooldown for amazon: 90 days" in _say(db, tg, "/cooldown amazon 90")[2]
    assert "Not saved" in _say(db, tg, "/cooldown amazon 5000")[2]
    (g,) = tracker.by_company(db, "amazon")
    assert g["dream"] and g["cooldown_days"] == 90 and g["in_cooldown"]
    out = _say(db, tg, "/company amazon")[2]
    assert "⏳ wait until" in out and "SDE I" in out and "resume: another resume" in out
    assert "SDE I, AWS" in out, "open jobs at that company (Amazon Web Services counts as Amazon)"
    assert f"t:a:{a2.id}" not in out
    assert "🗂 Your applications by company (1)" in _say(db, tg, "/applied")[2]
    assert "removed" in _say(db, tg, "/cooldown amazon off")[2]
    assert tracker.cooldown_days(db, "Amazon") is None


def test_dashboard_tracker_page_and_forms(world, client, db):  # noqa: F811
    j = job(db, "https://www.amazon.jobs/en/jobs/1", title="SDE I", company="Amazon")
    other = job(db, "https://example.com/2", title="Backend Engineer", company="Examplecorp")
    page = client.get("/tracker")
    assert page.status_code == 200 and "https://www.amazon.jobs/en/jobs/1" in page.text and "⭐" in page.text
    assert "Backend Engineer" not in client.get("/tracker?q=amazon").text
    future = (utcnow() + timedelta(days=3)).date().isoformat()
    r = client.post(f"/tracker/jobs/{j.id}/applied", data={"resume_version_id": str(_version(db).id),
                                                            "applied_on": future}, follow_redirects=False)
    assert "future" in r.headers["location"]
    r = client.post(f"/tracker/jobs/{j.id}/applied", data={"resume_version_id": str(_version(db).id),
                                                            "applied_on": "2026-10-01"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    db.expire_all()
    app = apps.for_job(db, j.id)
    assert app.status == "applied" and f"{app.applied_at:%Y-%m-%d}" == "2026-10-01"
    client.post(f"/tracker/jobs/{other.id}/later", data={"action": "not_interested"})
    client.post("/tracker/cooldown", data={"company": "Amazon", "days": "60"})
    db.expire_all()
    assert tracker.cooldown_days(db, "amazon") == 60 and db.get(type(other), other.id).user_status == "dismissed"
    assert "Master resume v1" in client.get("/tracker?q=amazon").text


# ---- employer emails -----------------------------------------------------------------------------------------

def _applied(db, company="Examplecorp", title="Software Engineer", days_ago=5):
    j = job(db, f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{title.replace(' ', '')}", title=title,
            company=company)
    return tracker.record_self_applied(db, j, None, applied_on=(utcnow() - timedelta(days=days_ago)).date())


def test_employer_emails_update_status_or_ask(gmail_ready, db):
    from jobpilot.integrations import gmail

    rejected, unclear = _applied(db), _applied(db, company="Othercorp", title="Backend Engineer")
    db.commit()
    now = utcnow()
    inbox = [
        {"id": "m1", "from": "Examplecorp <deals@examplecorp.com>", "subject": "Unfortunately your order is delayed", "date": now},
        {"id": "m2", "from": "Examplecorp Recruiting <no-reply@greenhouse-mail.io>",
         "subject": "Unfortunately, we will not be moving forward with your application", "date": now},
        {"id": "m3", "from": "Othercorp Recruiting <no-reply@greenhouse-mail.io>", "subject": "Your application to Othercorp", "date": now},
        {"id": "m4", "from": "Othercorp Recruiting <no-reply@greenhouse-mail.io>", "subject": "Thank you for applying to Othercorp", "date": now},
        {"id": "m5", "from": "Unrelated <x@unrelated.example>", "subject": "Interview invitation", "date": now},
    ]
    client = gmail.GmailClient(gmail_ready, http=InboxGmail(inbox))
    res = tracker.detect_outcome_emails(db, gmail_ready, client)
    db.commit()
    assert (res["checked"], res["updated"], res["asked"]) == (5, 1, 1)
    db.refresh(rejected), db.refresh(unclear)
    assert rejected.status == "rejected" and rejected.closed_at, "a clear rejection subject updates it"
    assert unclear.status == "applied", "an unclear subject changes nothing"
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "email_outcome"))
    assert item.entity_id == unclear.id and "Your application to Othercorp" in item.reason
    again = tracker.detect_outcome_emails(db, gmail_ready, client)
    assert again["checked"] == 0, "each message is looked at once"

    # notifications: the automatic update carries Undo; the question carries the choices
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"daily_summary": False, "apply_check": False}})
    notifications.scan(db, gmail_ready)
    db.commit()
    outs = [n for n in db.scalars(select(Notification).where(Notification.kind == "email_outcome"))]
    undo = next(n for n in outs if "Undo" in str(n.reply_markup))
    assert f"u:{rejected.id}" in str(undo.reply_markup) and "rejected" in undo.text
    ask = next(n for n in outs if n is not undo)
    assert f"o:{item.id}:interview" in str(ask.reply_markup)
    status_msgs = " ".join(n.text for n in db.scalars(select(Notification).where(Notification.kind == "application_status")))
    assert "→ rejected" not in status_msgs, "announced once, with its Undo"


def test_undo_and_answering_an_email_question(tg, db):  # noqa: F811
    app = _applied(db)
    assert tracker.apply_outcome(db, app, "rejected", "Unfortunately …\n[headers]")
    db.commit()
    assert "back to applied" in _cb(db, tg, f"u:{app.id}")[2]
    db.refresh(app)
    assert app.status == "applied" and app.closed_at is None
    assert "Nothing to undo" in _cb(db, tg, f"u:{app.id}")[2], "only automatic updates can be undone"

    from jobpilot.services import review

    item = review.open_item(db, "email_outcome", "application", app.id, "Email from Examplecorp — what happened?",
                            "…", data={"subject": "Your application", "from": "x", "message_id": "m9"})
    db.commit()
    assert "Updated" in _cb(db, tg, f"o:{item.id}:interview")[2]
    db.refresh(app)
    assert app.status == "interview"
    assert "already answered" in _cb(db, tg, f"o:{item.id}:offer")[2]
    assert "not recognised" in _cb(db, tg, f"o:{item.id}:hired")[2]
    ev = db.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
                    .order_by(ApplicationEvent.id.desc())).first()
    assert ev.actor == "user"


def test_apply_checks_are_paced_per_day(tg, db):  # noqa: F811
    fresh = job(db, "https://example.com/new", title="Found just now")
    for i in range(8):
        job(db, f"https://example.com/{i}", title=f"Engineer {i}").first_seen_at = utcnow() - timedelta(days=2)
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"daily_summary": False, "relevant_digest": False,
                                                       "high_match": False, "apply_check": True}})
    notifications.scan(db, tg)
    notifications.scan(db, tg)
    db.commit()
    asks = list(db.scalars(select(Notification).where(Notification.kind == "apply_check")))
    assert len(asks) == settings_store.DEFAULTS["tracker"]["ask_daily"] == 5
    kb = str(asks[0].reply_markup)
    assert "t:a:" in kb and "t:n:" in kb and "Apply" in kb
    assert asks[0].text.startswith("🙋 Did you apply?")
    assert not any("Found just now" in n.text for n in asks), "a job found today is not asked about yet"
    assert fresh in tracker.to_ask(db, limit=10), "but /track lists it"


# ---- Apple source ----------------------------------------------------------------------------------------------

def _apple_page(section: str, data: dict) -> str:
    import json

    blob = json.dumps(json.dumps({"loaderData": {"root": {}, section: data}}))
    return f"<html><script>window.__staticRouterHydrationData = JSON.parse({blob});</script></html>"


def test_apple_adapter_reads_search_and_details(settings):
    import pytest

    from jobpilot.sources.careers import AppleJobsAdapter
    from jobpilot.sources.http import FetchError
    from tests.helpers import make_http

    def row(i, title):
        return {"id": f"2006{i}-0321", "postingTitle": title, "transformedPostingTitle": title.lower().replace(" ", "-"),
                "locations": [{"name": "Bengaluru", "countryName": "India"}], "jobSummary": "Summary.",
                "postDateInGMT": "2026-10-05T21:11:45Z", "team": {"teamName": "Software and Services"}}

    hyd = {**row(1, "Software Engineer"), "id": "20061-1052", "locations": [{"name": "Hyderabad"}]}
    search = _apple_page("search", {"searchResults": [row(1, "Software Engineer"), row(2, "Senior Software Engineer"),
                                                      row(3, "Genius"), hyd], "totalRecords": 4})
    detail = _apple_page("jobDetails", {"jobsData": {
        "jobSummary": "Build services.", "description": "Based in Bengaluru.",
        "minimumQualifications": "BS in Computer Science\n1+ years of Java", "preferredQualifications": "Kafka",
        "locations": [{"city": "Bengaluru", "countryName": "India"}], "postDateInGMT": "2026-10-05T21:11:45Z"}})
    base = AppleJobsAdapter.BASE
    http = make_http(settings, {f"{base}/search": (200, search), f"{base}/details/*": (200, detail)})
    res = AppleJobsAdapter().fetch(http, {"queries": ["software engineer"], "title_keywords": ["software engineer"],
                                          "exclude_keywords": ["senior"]}, None)
    (j,) = res.items  # the Hyderabad posting of the same requisition is the same job
    assert j.company_name == "Apple" and j.location == "Bengaluru, India" and j.raw["locations"] == ["Bengaluru", "Hyderabad"]
    assert j.apply_url == f"{base}/details/20061-0321/software-engineer"
    assert "Minimum Qualifications" in j.description_raw and "1+ years of Java" in j.description_text
    assert not res.full_snapshot, "a search is a subset: nothing is expired because it's missing"
    with pytest.raises(FetchError):
        AppleJobsAdapter().fetch(make_http(settings, {f"{base}/search": (200, "<html>changed</html>")}),
                                 {"queries": ["x"], "title_keywords": ["x"]}, None)
    with pytest.raises(ValueError):
        AppleJobsAdapter().validate_config({"queries": ["x"], "title_keywords": ["x"], "location": "india/../x"})
