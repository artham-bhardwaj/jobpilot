"""Milestone 25: what the bot actually says (synthetic data; fake token; nothing is sent)."""
from __future__ import annotations

import json
import time

import pytest

from jobpilot.models import Job, utcnow
from jobpilot.services import telegram_bot
from tests.apply_fixtures import TOKEN, job, tg, world  # noqa: F401


def _ask(db, tg, text):  # noqa: F811
    upd = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 4242, "type": "private"}, "text": text}}
    return telegram_bot.handle_update(db, tg, upd)


def test_jobs_shows_eligibility_so_a_high_score_is_not_read_as_eligible(tg, db):  # noqa: F811
    job(db, "https://job-boards.greenhouse.io/x/jobs/1", title="Java Backend Engineer", score=90, eligibility="ineligible")
    job(db, "https://job-boards.greenhouse.io/x/jobs/2", title="SDE I Java", score=80, eligibility="likely_eligible")
    job(db, "https://job-boards.greenhouse.io/x/jobs/3", title="Backend Engineer", score=70, eligibility="unknown")
    _, _, text = _ask(db, tg, "/jobs")
    lines = [ln for ln in text.splitlines() if ln.startswith("• ")]  # each job also has an "apply:" line
    assert lines[0].startswith("• ⛔ 90 Java Backend Engineer") and lines[1].startswith("• ✅ 80 SDE I Java")
    assert lines[2].startswith("• ❓ 70 Backend Engineer") and "relevance, not eligibility" in text
    assert "   apply: https://job-boards.greenhouse.io/x/jobs/1" in text


def test_status_counts_are_readable(tg, db):  # noqa: F811
    _, _, text = _ask(db, tg, "/status")
    assert "Notifications: none yet" in text and "{" not in text


def test_no_command_reply_contains_secrets_or_resume_text(tg, db):  # noqa: F811
    from jobpilot.models import ResumeVersion
    from jobpilot.services import notifications, settings_store

    tg.gmail_enabled = True
    tg.ensure_dirs()
    oauth = {"access_token": "ya29.SYNTHETIC-ACCESS", "refresh_token": "1//0SYNTHETIC-REFRESH", "expires_at": time.time() + 3600,
             "email": "test.candidate@example.com", "scope": "https://www.googleapis.com/auth/gmail.compose"}
    tg.gmail_token_file.write_text(json.dumps(oauth))
    tg.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "CS-SYNTHETIC"}}))
    v = db.query(ResumeVersion).first()
    v.extracted_text = "Test Candidate · +91 90000 00000\nBuilt a private synthetic payments ledger for Examplecorp clients"
    for i in range(3):
        job(db, f"https://job-boards.greenhouse.io/x/jobs/{i}", title=f"Java Backend Engineer {i}")
    notifications.scan(db, tg)
    db.commit()
    forbidden = [TOKEN, TOKEN.split(":")[1], "ya29.SYNTHETIC-ACCESS", "1//0SYNTHETIC-REFRESH", "CS-SYNTHETIC",
                 "+91 90000 00000", "private synthetic payments ledger"]
    for cmd in telegram_bot.COMMANDS:
        if cmd in ("/pause", "/resume"):
            continue
        _, _, text = _ask(db, tg, cmd)
        assert text and not any(f in text for f in forbidden), cmd


def test_pause_and_resume_change_only_the_outreach_pause_flag(tg, db):  # noqa: F811
    from sqlalchemy import func, select

    from jobpilot.models import Application, BrowserRun
    from jobpilot.services import outreach_auto, settings_store

    before = settings_store.get(db, "outreach")
    assert "paused" in _ask(db, tg, "/pause")[2] and outreach_auto.is_paused(db)
    assert "paused" in outreach_auto.run(db, tg)["message"] or not before["auto_enabled"]
    assert "resumed" in _ask(db, tg, "/resume")[2].lower() and not outreach_auto.is_paused(db)
    assert settings_store.get(db, "outreach") == before
    assert db.scalar(select(func.count(Application.id))) == 0 and db.scalar(select(func.count(BrowserRun.id))) == 0


def test_jobs_shows_each_jobs_region(tg, db):  # noqa: F811
    job(db, "https://job-boards.greenhouse.io/x/jobs/1", title="Java Backend Engineer", location="Bengaluru, India")
    job(db, "https://job-boards.greenhouse.io/x/jobs/2", title="SDE I Java", location="Dubai, United Arab Emirates")
    text = _ask(db, tg, "/jobs")[2]
    assert "(India)" in text and "(Dubai / UAE)" in text


# ---- high-match alerts: eligible / unknown yes, ineligible to the digest ----------------------------------

def _scan_new(db, tg):  # noqa: F811
    from sqlalchemy import select

    from jobpilot.models import Notification
    from jobpilot.services import notifications, settings_store

    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"daily_summary": False, "relevant_digest": True}})
    before = {n.id for n in db.scalars(select(Notification))}
    notifications.scan(db, tg)
    db.commit()
    return [n for n in db.scalars(select(Notification)) if n.id not in before]


def test_high_match_alerts_skip_ineligible_jobs(tg, db):  # noqa: F811
    eligible = job(db, "https://job-boards.greenhouse.io/x/jobs/1", title="Java Backend Engineer", score=90,
                   eligibility="likely_eligible")
    unknown = job(db, "https://job-boards.greenhouse.io/x/jobs/2", title="SDE I Java", score=88, eligibility="unknown")
    ineligible = job(db, "https://job-boards.greenhouse.io/x/jobs/3", title="Senior Backend Engineer", score=92,
                     eligibility="ineligible")
    new = _scan_new(db, tg)
    highs = [n for n in new if n.kind == "high_match"]
    assert len(highs) == 2
    by_title = {("Java Backend Engineer" in n.text, "SDE I Java" in n.text): n.text for n in highs}
    assert by_title[(True, False)].startswith("🎯 High match\n") and "eligibility: likely_eligible" in by_title[(True, False)]
    assert by_title[(False, True)].startswith("🎯 High match — eligibility unknown: review the requirements")
    assert not any("Senior Backend Engineer" in n.text for n in highs), "no high-match alert for an ineligible job"
    (digest,) = [n for n in new if n.kind == "relevant_digest"]
    assert "⛔ ineligible · 92 · Senior Backend Engineer" in digest.text and "eligibility: ineligible" in digest.text
    assert _scan_new(db, tg) == [], "deduplicated: nothing is announced twice"
    del eligible, unknown, ineligible


def test_ineligible_job_already_alerted_is_not_announced_again(tg, db):  # noqa: F811
    from jobpilot.models import NotificationKey

    j = job(db, "https://job-boards.greenhouse.io/x/jobs/1", title="Senior Backend Engineer", score=92,
            eligibility="ineligible")
    db.add(NotificationKey(key=f"job:{j.id}:high", notification_id=None))  # delivered under the old rule
    db.commit()
    assert [n for n in _scan_new(db, tg) if "Senior Backend Engineer" in n.text] == []


def test_job_that_becomes_eligible_later_still_gets_its_high_match_alert(tg, db):  # noqa: F811
    j = job(db, "https://job-boards.greenhouse.io/x/jobs/1", title="Backend Engineer", score=90, eligibility="ineligible")
    assert [n.kind for n in _scan_new(db, tg)] == ["relevant_digest"]
    j.eligibility = "likely_eligible"  # e.g. after you add a confirmed skill and it is re-scored
    db.commit()
    assert [n.kind for n in _scan_new(db, tg)] == ["high_match"]


def test_jobs_marks_applications_hard_conflicts_and_regions(tg, db):  # noqa: F811
    from jobpilot.services import applications as apps

    a = job(db, "https://job-boards.greenhouse.io/x/jobs/1", title="Java Backend Engineer", score=90)
    job(db, "https://examplecorp.wd5.myworkdayjobs.com/x/job/1", title="SDE I", score=85, eligibility="likely_eligible")
    job(db, "https://job-boards.greenhouse.io/x/jobs/3", title="Lead Engineer", score=80, eligibility="ineligible",
        hard_conflict=True)
    app_ = apps.create(db, a)
    db.commit()
    text = _ask(db, tg, "/jobs")[2]
    assert f"application #{app_.id}: preparing)" in text
    assert "SDE I — Examplecorp (Elsewhere)" in text, "no location: neither India, remote nor Dubai"
    assert "Lead Engineer — Examplecorp (Elsewhere; hard conflict)" in text
