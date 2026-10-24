"""Milestone 27: less manual work — answering questions from Telegram, confirmation emails recorded automatically.

Synthetic data, fake Telegram updates (handler level) and a fake Gmail; nothing is sent anywhere.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta
from email.utils import format_datetime

import pytest
from sqlalchemy import func, select

from jobpilot.models import (AnswerBankEntry, ApplicationEvent, ApplicationEvidence, ApplicationQuestion, BrowserRun,
                             ReviewItem, utcnow)
from jobpilot.services import applications as apps
from jobpilot.services import telegram_bot
from tests.test_m13_gmail import FakeGmail
from tests.apply_fixtures import job, tg, world  # noqa: F401


def _say(db, settings, text, chat=4242):
    upd = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat, "type": "private"}, "text": text}}
    return telegram_bot.handle_update(db, settings, upd)


def _blocked_on_questions(db, settings):
    """An application whose form asked two questions JobPilot had no approved answer for (as the browser helper
    records them)."""
    from jobpilot.services import answers as answer_svc

    app_ = apps.create(db, job(db, "https://job-boards.greenhouse.io/x/jobs/1"))
    answer_svc.add_questions(db, app_, ["What is your current country of residence?",
                                        "Will you now or in the future require visa sponsorship?"], source="browser")
    db.commit()
    return app_


# ---- answering from Telegram ------------------------------------------------------------------------------------

def test_answering_in_telegram_answers_the_application_questions(tg, db):  # noqa: F811
    app_ = _blocked_on_questions(db, tg)
    qs = db.scalars(select(ApplicationQuestion).where(ApplicationQuestion.application_id == app_.id)
                    .order_by(ApplicationQuestion.id)).all()
    country, visa = qs
    assert visa.sensitive and not country.sensitive
    listing = _say(db, tg, "/questions")[2]
    assert f"Q{country.id}: What is your current country of residence?" in listing and "🔒" in listing

    reply = _say(db, tg, f"/answer Q{country.id} India +bank")[2]
    assert "answered" in reply and "Saved to the answer bank" in reply and "1 question(s) still open" in reply
    entry = db.scalar(select(AnswerBankEntry).where(AnswerBankEntry.answer == "India"))
    assert entry.approved and not entry.sensitive

    reply = _say(db, tg, f"/answer {visa.id} No +bank")[2]
    assert "Not saved to the answer bank" in reply and "All questions" in reply
    assert db.scalar(select(func.count(AnswerBankEntry.id)).where(AnswerBankEntry.answer == "No")) == 0
    db.refresh(visa)
    assert visa.status == "approved" and visa.answer == "No" and visa.answer_source == "manual"
    db.refresh(app_)
    assert app_.applied_at is None, "answering never submits anything"


def test_answer_command_is_safe(tg, db):  # noqa: F811
    app_ = _blocked_on_questions(db, tg)
    q = db.scalar(select(ApplicationQuestion).where(ApplicationQuestion.application_id == app_.id))
    assert _say(db, tg, f"/answer Q{q.id} India", chat=999)[0] == "rejected", "only your chat"
    assert "Usage" in _say(db, tg, "/answer")[2] and "Usage" in _say(db, tg, "/answer India")[2]
    assert "not open" in _say(db, tg, "/answer Q99999 India")[2]
    apps.transition(db, app_, "withdrawn")
    assert "not open" in _say(db, tg, f"/answer Q{q.id} India")[2], "closed applications can't be changed"
    db.refresh(q)
    assert q.status == "unanswered" and q.answer is None


# ---- confirmation emails ----------------------------------------------------------------------------------------

class InboxGmail(FakeGmail):
    """FakeGmail plus an inbox: messages.list (labelIds, no query) and messages.get (metadata)."""

    def __init__(self, messages):
        super().__init__()
        self.inbox = messages

    def _handle(self, method, path, kw):
        if path == "/messages" and method == "GET":
            assert "q" not in (kw.get("params") or {}), "the metadata scope does not allow searching"
            return self._resp(200, {"messages": [{"id": m["id"]} for m in self.inbox]})
        if path.startswith("/messages/") and method == "GET":
            assert (kw.get("params") or {}).get("format") == "metadata", "message bodies are never fetched"
            m = next(x for x in self.inbox if x["id"] == path.split("/")[-1])
            return self._resp(200, {"id": m["id"], "payload": {"headers": [
                {"name": "From", "value": m["from"]}, {"name": "Subject", "value": m["subject"]},
                {"name": "Date", "value": format_datetime(m["date"].replace(tzinfo=__import__("datetime").timezone.utc))}]}})
        return super()._handle(method, path, kw)


@pytest.fixture
def gmail_ready(world):  # noqa: F811
    world.gmail_enabled = True
    world.ensure_dirs()
    world.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    world.gmail_token_file.write_text(json.dumps({
        "access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600, "email": "test.candidate@example.com",
        "scope": "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.metadata"}))
    return world


def test_confirmation_email_records_the_submission_once(gmail_ready, db):
    from jobpilot.integrations import gmail

    opened = job(db, "https://job-boards.greenhouse.io/examplecorp/jobs/1", title="Software Engineer",
                 company="Examplecorp")
    never_opened = job(db, "https://job-boards.greenhouse.io/othercorp/jobs/2", title="Backend Engineer",
                       company="Othercorp")
    a1, a2 = apps.create(db, opened), apps.create(db, never_opened)
    started = utcnow() - timedelta(hours=2)
    db.add(BrowserRun(application_id=a1.id, status="closed", started_at=started))  # you submitted in assisted mode
    db.commit()
    now = utcnow()
    inbox = [
        {"id": "m1", "from": "Examplecorp <promo@examplecorp-cards.com>", "subject": "Thank you for applying for your Examplecorp card", "date": now},
        {"id": "m2", "from": "Examplecorp Recruiting <no-reply@us.greenhouse-mail.io>", "subject": "Update on your application to Examplecorp", "date": now},
        {"id": "m3", "from": "Examplecorp Recruiting <no-reply@us.greenhouse-mail.io>", "subject": "Thank you for applying to Examplecorp", "date": started - timedelta(days=3)},
        {"id": "m4", "from": "Othercorp <no-reply@us.greenhouse-mail.io>", "subject": "Thank you for applying to Othercorp", "date": now},
        {"id": "m5", "from": "Examplecorp Recruiting <no-reply@us.greenhouse-mail.io>", "subject": "Thank you for applying to Examplecorp!", "date": now},
    ]
    fake = InboxGmail(inbox)
    client = gmail.GmailClient(gmail_ready, http=fake)
    res = gmail.detect_confirmation_emails(db, gmail_ready, client)
    db.commit()
    assert res["recorded"] == 1 and res["checked"] == 5
    db.refresh(a1)
    db.refresh(a2)
    assert a1.status == "applied" and a1.confirmation_method == "confirmation_email" and a1.applied_at is not None
    ev = db.scalar(select(ApplicationEvidence).where(ApplicationEvidence.application_id == a1.id))
    assert ev.kind == "confirmation_email" and "Thank you for applying to Examplecorp!" in ev.text and "m5" in ev.text
    last = db.scalar(select(ApplicationEvent).where(ApplicationEvent.application_id == a1.id).order_by(ApplicationEvent.id.desc()))
    assert (last.to_status, last.actor) == ("applied", "system")
    assert a2.status == "preparing" and a2.applied_at is None, "never opened by JobPilot: not touched"
    # the same messages are never processed twice, and nothing was sent
    again = gmail.detect_confirmation_emails(db, gmail_ready, client)
    assert again["checked"] == 0 and again["recorded"] == 0
    assert fake.count("POST", "/drafts/send") == 0 and fake.count("POST", "/messages/send") == 0
    assert not any("format" in str(kw.get("params")) and "full" in str(kw.get("params")) for _, _, kw in fake.calls)


def test_two_matching_applications_are_left_for_you(gmail_ready, db):
    from jobpilot.integrations import gmail

    a = apps.create(db, job(db, "https://job-boards.greenhouse.io/examplecorp/jobs/1", title="Software Engineer"))
    b = apps.create(db, job(db, "https://job-boards.greenhouse.io/examplecorp/jobs/2", title="Backend Engineer"))
    for x in (a, b):
        db.add(BrowserRun(application_id=x.id, status="closed", started_at=utcnow() - timedelta(hours=1)))
    db.commit()
    fake = InboxGmail([{"id": "m1", "from": "Examplecorp <no-reply@us.greenhouse-mail.io>",
                        "subject": "Thank you for applying to Examplecorp", "date": utcnow()}])
    res = gmail.detect_confirmation_emails(db, gmail_ready, gmail.GmailClient(gmail_ready, http=fake))
    assert res["recorded"] == 0 and res["ambiguous"] == 1
    db.refresh(a)
    db.refresh(b)
    assert a.applied_at is None and b.applied_at is None


def test_gmail_sync_task_runs_the_detector(gmail_ready, db, monkeypatch):
    from jobpilot.integrations import gmail
    from jobpilot.services import scheduler

    monkeypatch.setattr(gmail, "GmailClient", lambda settings: object())
    monkeypatch.setattr(gmail, "sync_replies", lambda s, st, c: {"message": "0 replies"})
    monkeypatch.setattr(gmail, "detect_confirmation_emails", lambda s, st, c: {"message": "1 submission(s) confirmed"})
    out = scheduler._gmail_sync(gmail_ready)(db)
    assert out["message"] == "0 replies; 1 submission(s) confirmed; no submitted applications to track", \
        "the employer-email outcome check runs too"


def test_bank_entry_is_updated_not_duplicated(tg, db):  # noqa: F811
    app_ = _blocked_on_questions(db, tg)
    q = db.scalars(select(ApplicationQuestion).where(ApplicationQuestion.application_id == app_.id)
                   .order_by(ApplicationQuestion.id)).first()
    _say(db, tg, f"/answer Q{q.id} India +bank")
    _say(db, tg, f"/answer Q{q.id} Republic of India +bank")
    entries = db.scalars(select(AnswerBankEntry).where(AnswerBankEntry.question == q.question)).all()
    assert len(entries) == 1 and entries[0].answer == "Republic of India" and entries[0].approved


def test_a_refused_assist_run_does_not_count_as_an_opened_form(gmail_ready, db):
    from jobpilot.integrations import gmail

    a = apps.create(db, job(db, "https://job-boards.greenhouse.io/examplecorp/jobs/1", title="Software Engineer"))
    db.add(BrowserRun(application_id=a.id, status="failed", message="Not approved: opening job-boards.greenhouse.io",
                      started_at=utcnow() - timedelta(hours=1)))
    db.commit()
    fake = InboxGmail([{"id": "m1", "from": "Examplecorp <no-reply@us.greenhouse-mail.io>",
                        "subject": "Thank you for applying to Examplecorp", "date": utcnow()}])
    res = gmail.detect_confirmation_emails(db, gmail_ready, gmail.GmailClient(gmail_ready, http=fake))
    assert res["recorded"] == 0 and "no opened" in res["message"] and fake.calls == []
    db.refresh(a)
    assert a.applied_at is None
