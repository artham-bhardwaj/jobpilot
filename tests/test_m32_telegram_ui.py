"""Milestone 32: Telegram formatting and buttons (fake Telegram; synthetic data)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from jobpilot.models import ApplicationQuestion, Notification
from jobpilot.services import applications as apps
from jobpilot.services import notifications, settings_store, telegram_bot, telegram_ui
from tests.test_m14_telegram import FakeTelegram
from tests.apply_fixtures import TOKEN, job, tg, world  # noqa: F401


def test_formatting_escapes_everything_from_the_data():
    out = telegram_ui.to_html("🎯 High match <script>alert(1)</script>\nCompany: A&B <Corp>\n• run `jobpilot assist 1`")
    assert out.startswith("<b>🎯 High match &lt;script&gt;alert(1)&lt;/script&gt;</b>")
    assert "<b>Company:</b> A&amp;B &lt;Corp&gt;" in out and "<code>jobpilot assist 1</code>" in out
    assert "<script>" not in out


def test_html_with_plain_fallback(tg):  # noqa: F811
    from jobpilot.integrations import telegram

    fake = FakeTelegram()
    client = telegram.TelegramClient(tg, http=fake, sleep=lambda s: None)
    client.send_text("Heading\nbody", html=True)
    (p,) = [p for m, p in fake.calls if m == "sendMessage"]
    assert p["parse_mode"] == "HTML" and p["text"] == "<b>Heading</b>\nbody"
    fake.queue["sendMessage"] = [(400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})]
    client.send_text("Heading\nbody", html=True)
    last = [p for m, p in fake.calls if m == "sendMessage"][-1]
    assert "parse_mode" not in last and last["text"] == "Heading\nbody", "same text, plain"


def _press(data, chat="4242", user="4242"):
    return {"id": "cb", "from": {"id": int(user)}, "data": data, "message": {"chat": {"id": int(chat), "type": "private"}}}


def test_menu_buttons_show_the_same_views_and_are_authorized(tg, db):  # noqa: F811
    assert telegram_bot.handle_callback(db, tg, _press("m:jobs", user="999"))[0] == "rejected"
    res, command, reply = telegram_bot.handle_callback(db, tg, _press("m:status"))
    assert command == "m:status" and reply.startswith("🩺 JobPilot status")
    assert "paused" in telegram_bot.handle_callback(db, tg, _press("m:pause"))[2]
    assert "resumed" in telegram_bot.handle_callback(db, tg, _press("m:resume"))[2].lower()
    assert "not recognised" in telegram_bot.handle_callback(db, tg, _press("m:delete_everything"))[2]
    assert telegram_ui.menu()["inline_keyboard"][0][0]["callback_data"] == "m:status"


def _question(db, text):
    """An open application (one per test database) with one unanswered question."""
    from jobpilot.models import Application

    a = db.scalar(select(Application))
    if a is None:
        a = apps.create(db, job(db, "https://job-boards.greenhouse.io/x/jobs/1"))
    q = ApplicationQuestion(application_id=a.id, question=text, status="unanswered", source="browser")
    db.add(q)
    db.commit()
    return q


def test_answer_button_then_reply_answers_that_question(tg, db):  # noqa: F811
    q = _question(db, "What is your current country of residence?")
    res, command, prompt = telegram_bot.handle_callback(db, tg, _press(f"q:{q.id}"))
    assert command == f"q:{q.id}" and prompt.startswith(f"✍️ Answer Q{q.id}")
    assert telegram_bot._reply_markup(db, command) == {"force_reply": True,
                                                       "input_field_placeholder": f"Your answer for Q{q.id}"}
    reply = {"update_id": 9, "message": {"message_id": 9, "chat": {"id": 4242, "type": "private"}, "text": "India",
                                         "reply_to_message": {"from": {"is_bot": True}, "text": prompt}}}
    assert "answered" in telegram_bot.handle_update(db, tg, reply)[2]
    db.refresh(q)
    assert q.status == "approved" and q.answer == "India"
    assert "no longer open" in telegram_bot.handle_callback(db, tg, _press(f"q:{q.id}"))[2]
    stranger = {**reply, "message": {**reply["message"], "chat": {"id": 999, "type": "private"}}}
    assert telegram_bot.handle_update(db, tg, stranger)[0] == "rejected"


def test_legal_questions_get_no_answer_button(tg, db):  # noqa: F811
    legal = _question(db, "Please review the NDA and indicate your agreement by typing your full name below")
    plain = _question(db, "Have you worked here before?")
    assert "legal acknowledgement" in telegram_bot.handle_callback(db, tg, _press(f"q:{legal.id}"))[2]
    kb = telegram_ui.notification_markup("questions", [
        {"question_id": legal.id, "question": legal.question}, {"question_id": plain.id, "question": plain.question}])
    datas = [b["callback_data"] for r in kb["inline_keyboard"] for b in r if "callback_data" in b]
    assert f"q:{plain.id}" in datas and f"q:{legal.id}" not in datas
    cmd = telegram_ui.command_markup(db, "/questions")
    assert f"q:{legal.id}" not in str(cmd) and f"q:{plain.id}" in str(cmd)


def test_notifications_get_buttons(tg, db):  # noqa: F811
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"), "kinds": {"daily_summary": False}})
    job(db, "https://job-boards.greenhouse.io/x/jobs/1", score=90)
    notifications.scan(db, tg)
    db.commit()
    by_kind = {n.kind: n for n in db.scalars(select(Notification))}
    high = by_kind["high_match"].reply_markup["inline_keyboard"]
    assert high[0][0] == {"text": "🔗 Open posting", "url": "https://job-boards.greenhouse.io/x/jobs/1"}
    assert "submission_batch" not in by_kind, "JobPilot never asks to approve automatic submissions"
    assert telegram_ui.url_button("x", "http://127.0.0.1:8765/") == [], "only public https links become buttons"


def test_command_replies_over_polling_carry_buttons(tg, app, db):  # noqa: F811
    from jobpilot.integrations import telegram
    from tests.test_m14_telegram import msg

    fake = FakeTelegram()
    fake.updates = [msg(1, text_="/menu"), msg(2, text_="/start"), msg(3, text_="/jobs")]
    client = telegram.TelegramClient(tg, http=fake, sleep=lambda s: None)
    telegram_bot.poll_once(tg, app.state.sessionmaker, client, poll_timeout=0)
    sent = fake.sent()
    assert sent[0]["reply_markup"] == telegram_ui.menu() and sent[0]["text"].startswith("🧭 JobPilot")
    assert sent[1]["reply_markup"]["keyboard"][0] == ["/status", "/jobs", "/dream"]
    assert all(s.get("parse_mode") == "HTML" for s in sent)


def test_cli_menu_registers_commands_and_sends_the_keyboard(tg, monkeypatch, capsys):  # noqa: F811
    import json
    from types import SimpleNamespace

    from jobpilot import cli, cli_telegram
    from jobpilot.integrations import telegram

    fake = FakeTelegram()
    fake.queue["setMyCommands"] = [(200, {"ok": True, "result": True})]
    real_client = telegram.TelegramClient
    monkeypatch.setattr(telegram, "TelegramClient", lambda settings: real_client(settings, http=fake, sleep=lambda s: None))
    monkeypatch.setattr(cli, "_ctx", lambda: (tg, None, None))
    assert cli_telegram.cmd_telegram(SimpleNamespace(action="menu", chat_id=None, force=False, dry_run=False,
                                                     once=False)) == 0
    out = json.loads(capsys.readouterr().out)
    (cmds,) = [p for m, p in fake.calls if m == "setMyCommands"]
    names = [c["command"] for c in cmds["commands"]]
    assert "menu" in names and "answer" not in names and out["commands_registered"] == len(names) == len(set(names))
    sent = fake.sent()
    assert sent[0]["reply_markup"] == telegram_ui.reply_keyboard() and sent[1]["reply_markup"] == telegram_ui.menu()
