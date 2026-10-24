"""Milestone 14: Telegram notifications and read-only commands (Bot API faked in-process; synthetic data only)."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
import requests
from sqlalchemy import func, select, text

from jobpilot.integrations import telegram
from jobpilot.models import Notification, NotificationKey, TaskRun, TelegramUpdate, utcnow
from jobpilot.services import notifications, settings_store, telegram_bot
from tests.helpers import seed_profile

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs"
CHAT = "4242"


class FakeTelegram(requests.Session):
    """Records calls; answers from per-method queues of (status, body) or exceptions."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.queue: dict[str, list] = {}
        self.updates: list[dict] = []
        self.n = 0

    def post(self, url, json=None, timeout=None, **kw):
        method = url.rsplit("/", 1)[-1]
        assert TOKEN in url and url.startswith("https://api.telegram.org/bot")
        self.calls.append((method, json or {}))
        q = self.queue.get(method)
        if q:
            item = q.pop(0)
            if isinstance(item, BaseException):
                raise item
            return self._resp(*item)
        if method == "sendMessage":
            self.n += 1
            return self._resp(200, {"ok": True, "result": {"message_id": self.n}})
        if method == "getMe":
            return self._resp(200, {"ok": True, "result": {"id": 123456789, "username": "TestJobPilotBot", "first_name": "Bot"}})
        if method == "getUpdates":
            off = (json or {}).get("offset") or 0
            return self._resp(200, {"ok": True, "result": [u for u in self.updates if u["update_id"] >= off]})
        return self._resp(404, {"ok": False, "description": "Not Found"})

    def _resp(self, status, body):
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(body).encode()
        return r

    def sent(self):
        """Messages as the user reads them: formatting tags removed (``html`` keeps what was actually sent)."""
        import html as _html
        import re as _re

        out = []
        for m, p in self.calls:
            if m == "sendMessage":
                q = dict(p)
                if q.get("parse_mode") == "HTML":
                    q["html"] = q["text"]
                    q["text"] = _html.unescape(_re.sub(r"</?(?:b|code)>", "", q["text"]))
                out.append(q)
        return out


def msg(update_id, chat=CHAT, text_="/status"):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": int(chat), "type": "private"},
                                                "text": text_}}


@pytest.fixture
def tg(settings):
    settings.telegram_enabled = True
    settings.telegram_bot_token = TOKEN
    settings.telegram_chat_id = CHAT
    settings = settings.model_validate(settings.model_dump())  # run validators (token becomes a SecretStr)
    settings.ensure_dirs()
    return settings


@pytest.fixture
def fake():
    return FakeTelegram()


def client(settings, fake, **kw):
    return telegram.TelegramClient(settings, http=fake, sleep=lambda s: kw.get("sleeps", []).append(s), **{
        k: v for k, v in kw.items() if k != "sleeps"})


# ---- Configuration and secrets ----------------------------------------------------------------------------

def test_configuration_validation(settings):
    st = telegram.config_status(settings)
    assert not st["ready"] and "JOBPILOT_TELEGRAM_ENABLED is not true" in st["problems"]
    settings.telegram_enabled = True
    st = telegram.config_status(settings)
    assert "JOBPILOT_TELEGRAM_BOT_TOKEN is not set" in st["problems"] and any("CHAT_ID" in p for p in st["problems"])
    with pytest.raises(Exception, match="numeric"):
        type(settings)(_env_file=None, telegram_chat_id="@me")
    s2 = type(settings)(_env_file=None, telegram_enabled=True, telegram_bot_token="not-a-token", telegram_chat_id="1")
    assert any("does not look like" in p for p in telegram.config_status(s2)["problems"])
    with pytest.raises(telegram.TelegramError, match="not set"):
        telegram.TelegramClient(type(settings)(_env_file=None))


def test_token_is_never_shown(tg, fake):
    assert TOKEN not in repr(tg) and TOKEN not in str(tg.model_dump()) and TOKEN not in repr(telegram.TelegramClient(tg))
    assert TOKEN not in json.dumps(telegram.config_status(tg))
    assert telegram.redact(f"https://api.telegram.org/bot{TOKEN}/sendMessage failed") == \
        "https://api.telegram.org/bot<token>/sendMessage failed"
    fake.queue["sendMessage"] = [requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")] * 3
    with pytest.raises(telegram.TelegramError) as ei:
        client(tg, fake).send_text("hi")
    assert TOKEN not in str(ei.value) and "network error" in str(ei.value)


def test_log_files_never_contain_the_token(tg):
    import logging

    from jobpilot.logging_setup import configure_logging

    path = configure_logging(tg)
    logging.getLogger("jobpilot.test").warning("calling https://api.telegram.org/bot%s/getMe", TOKEN)
    try:
        raise RuntimeError(f"boom bot{TOKEN}")
    except RuntimeError:
        logging.getLogger("jobpilot.test").exception("failure")
    for h in logging.getLogger().handlers:
        h.flush()
    content = path.read_text()
    assert TOKEN not in content and "<token>" in content


# ---- Client behaviour --------------------------------------------------------------------------------------

def test_send_plain_text_to_configured_chat_and_split_long_messages(tg, fake):
    ids = client(tg, fake).send_text("hello")
    (payload,) = fake.sent()
    assert ids == [1] and payload["chat_id"] == CHAT and payload["text"] == "hello" and "parse_mode" not in payload
    long = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
    ids = client(tg, fake).send_text(long)
    chunks = [p["text"] for p in fake.sent()[1:]]
    assert len(ids) == len(chunks) >= 3 and all(len(c) <= 4096 for c in chunks)
    assert "\n".join(chunks) == long
    assert telegram.split_text("y" * 9000) == ["y" * 4096, "y" * 4096, "y" * 808]


def test_timeouts_and_server_errors_are_retried(tg, fake):
    sleeps: list = []
    fake.queue["sendMessage"] = [requests.Timeout("t"), (502, {"ok": False, "description": "Bad Gateway"})]
    assert client(tg, fake, sleeps=sleeps).send_text("x") == [1]
    assert len(fake.sent()) == 3 and sleeps == [2, 4]


def test_rate_limit_honours_retry_after(tg, fake):
    sleeps: list = []
    fake.queue["sendMessage"] = [(429, {"ok": False, "description": "Too Many Requests: retry after 7",
                                        "parameters": {"retry_after": 7}})]
    client(tg, fake, sleeps=sleeps).send_text("x")
    assert sleeps == [7]


def test_permanent_errors_are_not_retried_and_explain_the_fix(tg, fake):
    fake.queue["sendMessage"] = [(401, {"ok": False, "description": "Unauthorized"})]
    with pytest.raises(telegram.TelegramError, match="token is wrong or was revoked") as ei:
        client(tg, fake).send_text("x")
    assert not ei.value.retryable and len(fake.sent()) == 1
    fake.queue["sendMessage"] = [(400, {"ok": False, "description": "Bad Request: chat not found"})]
    with pytest.raises(telegram.TelegramError, match="CHAT_ID"):
        client(tg, fake).send_text("x")


# ---- Commands ----------------------------------------------------------------------------------------------

def test_unauthorized_chats_get_nothing(app, tg, fake):
    fake.updates = [msg(1, chat="999", text_="/jobs"), msg(2, chat="999", text_="/status")]
    stats = telegram_bot.poll_once(tg, app.state.sessionmaker, client(tg, fake), poll_timeout=0)
    assert stats["rejected"] == 2 and stats["answered"] == 0 and fake.sent() == []
    with app.state.sessionmaker() as s:
        assert {(u.update_id, u.authorized, u.result) for u in s.scalars(select(TelegramUpdate))} == {
            (1, False, "rejected"), (2, False, "rejected")}


def test_commands_answer_truthfully_from_the_database(app, tg, fake, db):
    fake.updates = [msg(1, text_="/start"), msg(2, text_="/jobs"), msg(3, text_="/applications"), msg(4, text_="/pending"),
                    msg(5, text_="/status"), msg(6, text_="/today"), msg(7, text_="/rm -rf /"), msg(8, text_="hello")]
    stats = telegram_bot.poll_once(tg, app.state.sessionmaker, client(tg, fake), poll_timeout=0)
    replies = [p["text"] for p in fake.sent()]
    assert stats["answered"] == 8 and all(p["chat_id"] == CHAT for p in fake.sent())
    assert "/status" in replies[0] and "read-only" in replies[0]
    assert replies[1].startswith("No active jobs"), "empty results are said plainly"
    assert replies[2].startswith("No applications recorded yet")
    assert "Drafts to review: 0" in replies[3]
    assert "Last successful job collection: never" in replies[4]
    assert "Jobs discovered today: 0" in replies[5] and "none today" in replies[5]
    assert replies[6].startswith("Unknown command /rm") and replies[7] == "I only understand commands. Send /menu for buttons, or /help."
    for r in replies:
        assert TOKEN not in r and "@" not in r.replace("@me", ""), "no addresses or secrets in replies"


def test_duplicate_updates_are_processed_once_and_offset_persists(app, tg, fake):
    fake.updates = [msg(10, text_="/help")]
    factory = app.state.sessionmaker
    telegram_bot.poll_once(tg, factory, client(tg, fake), poll_timeout=0)
    with factory() as s:
        assert notifications.get_state(s, telegram_bot.OFFSET_KEY) == {"offset": 11}
        s.execute(text("DELETE FROM integration_state"))  # simulate a lost offset: Telegram re-delivers update 10
        s.commit()
    stats = telegram_bot.poll_once(tg, factory, client(tg, fake), poll_timeout=0)
    assert stats["duplicates"] == 1 and len(fake.sent()) == 1, "never answered twice"
    offsets = [p.get("offset") for m, p in fake.calls if m == "getUpdates"]
    assert offsets == [None, None]
    telegram_bot.poll_once(tg, factory, client(tg, fake), poll_timeout=0)
    assert [p.get("offset") for m, p in fake.calls if m == "getUpdates"][-1] == 11


def test_only_one_poller_at_a_time(app, tg, fake):
    from jobpilot.services.tasks import TaskLocked, task_lock

    with task_lock(tg, "telegram_poll"):
        with pytest.raises(TaskLocked):
            telegram_bot.poll_once(tg, app.state.sessionmaker, client(tg, fake), poll_timeout=0)
    assert not [m for m, _ in fake.calls if m == "getUpdates"]


# ---- Notifications -----------------------------------------------------------------------------------------

def _job(db, title, score, company="Examplecorp"):
    from jobpilot.models import Job

    j = Job(title=title, company_name=company, status="active", user_status="new", relevance_score=score,
            eligibility="likely_eligible", fingerprint=f"k-{title}-{score}", first_seen_at=utcnow(), last_seen_at=utcnow(),
            source_url="https://boards.example.com/1")
    db.add(j)
    db.flush()
    return j


def _quiet_summary(db):
    # the digest is off by default (Telegram is for alerts); these tests check how it works when you turn it on
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"daily_summary": False, "relevant_digest": True}})


def test_first_scan_is_baseline_then_only_new_events_are_announced_once(db, tg):
    _quiet_summary(db)
    _job(db, "Old Java Role", 90)
    first = notifications.scan(db, tg)
    assert first == {"created": 0, "baseline": 1, "kinds": []}, "history is not announced"
    high = _job(db, "Java Backend Engineer", 88)
    _job(db, "Python Backend Engineer", 60)
    _job(db, "SDE I", 58)
    _job(db, "Barista", 10)
    res = notifications.scan(db, tg)
    assert res["created"] == 2 and res["kinds"] == ["high_match", "relevant_digest"]
    msgs = {n.kind: n for n in db.scalars(select(Notification))}
    assert "Java Backend Engineer" in msgs["high_match"].text and f"/jobs/{high.id}" in msgs["high_match"].text
    assert "Discovered listing" in msgs["high_match"].text
    digest = msgs["relevant_digest"].text
    assert "(2)" in digest and "Python Backend Engineer" in digest and "SDE I" in digest and "Barista" not in digest
    assert notifications.scan(db, tg)["created"] == 0, "same events: nothing new"
    assert db.scalar(select(func.count(Notification.id))) == 2


def test_disabled_kinds_are_never_sent_later(db, tg):
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"relevant_digest": False, "daily_summary": False}})
    notifications.scan(db, tg)
    _job(db, "Python Backend Engineer", 60)
    assert notifications.scan(db, tg)["created"] == 0
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"),
                                             "kinds": {"relevant_digest": True, "daily_summary": False}})
    assert notifications.scan(db, tg)["created"] == 0


def test_daily_summary_counts_are_real_and_sent_once_per_day(db, tg):
    seed_profile(db)
    notifications.scan(db, tg, now=utcnow().replace(hour=0))
    _job(db, "Java Backend Engineer", 88)
    db.add(TaskRun(task_name="collect_jobs", status="failed", message="boom"))
    db.flush()
    summary = notifications.daily_summary(db, tg)
    assert "Jobs discovered today: 1" in summary and "Strong matches (score ≥ 75, not ineligible): 1" in summary
    assert "Failed tasks today: 1 — collect_jobs" in summary and "Recruiter replies detected today: 0 — none" in summary
    settings_store.put(db, "notifications", {**settings_store.get(db, "notifications"), "daily_summary_hour": 0})
    notifications.scan(db, tg)
    notifications.scan(db, tg)
    assert db.scalar(select(func.count(Notification.id)).where(Notification.kind == "daily_summary")) == 1


def test_delivery_states_are_truthful(db, tg, fake):
    _quiet_summary(db)
    notifications.scan(db, tg)
    _job(db, "Java Backend Engineer", 90)
    _job(db, "Senior Java Engineer", 91, company="Other")
    notifications.scan(db, tg)
    fake.queue["sendMessage"] = [(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 30}})] * 3
    res = notifications.deliver(db, tg, client(tg, fake, max_retries=0), sleep=lambda s: None)
    assert res["deferred"] == 2 and res["sent"] == 0
    n = db.scalar(select(Notification).order_by(Notification.id))
    assert n.status == "pending" and n.attempts == 1 and n.next_attempt_at > utcnow() and "rate limited" in n.last_error
    assert notifications.deliver(db, tg, client(tg, fake), sleep=lambda s: None)["sent"] == 0, "not due yet"
    res = notifications.deliver(db, tg, client(tg, fake), now=utcnow() + timedelta(minutes=5), sleep=lambda s: None)
    assert res["sent"] == 2
    assert all(x.status == "sent" and x.external_ids and x.sent_at for x in db.scalars(select(Notification)))


def test_configuration_error_marks_failed_and_stops(db, tg, fake):
    _quiet_summary(db)
    notifications.scan(db, tg)
    _job(db, "Java Backend Engineer", 90)
    _job(db, "Java Engineer II", 92)
    notifications.scan(db, tg)
    fake.queue["sendMessage"] = [(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"})]
    res = notifications.deliver(db, tg, client(tg, fake), sleep=lambda s: None)
    assert res["failed"] == 1 and len(fake.sent()) == 1, "the rest are not hammered against the same error"
    assert db.scalar(select(func.count(Notification.id)).where(Notification.status == "pending")) == 1


def test_nothing_is_sent_when_telegram_is_not_configured(app, db, settings):
    res = notifications.run(db, settings)
    assert "not configured; skipped" in res["message"]
    assert db.scalar(select(func.count(NotificationKey.key))) == 0, "no baseline until Telegram is set up"


# ---- Scheduler -----------------------------------------------------------------------------------------------

def test_scheduler_runs_notify_after_collection_and_is_idempotent(app, tg, fake, monkeypatch, db):
    from jobpilot.services import scheduler

    settings_store.put(db, "scheduler", {"tasks": {"collect_jobs": {"enabled": True, "every_hours": 24},
                                                   "notify": {"enabled": True, "every_hours": 0.25}}})
    db.commit()
    monkeypatch.setattr(telegram.requests, "Session", lambda: fake)
    first = scheduler.run_due(tg, app.state.sessionmaker)
    assert list(first) == ["collect_jobs", "notify"] and set(first.values()) == {"success"}
    assert scheduler.run_due(tg, app.state.sessionmaker) == {}, "nothing is due again right after a run"
    with app.state.sessionmaker() as s:
        assert s.scalar(select(func.count(TaskRun.id))) == 2


def test_telegram_failure_never_stops_job_collection(app, tg, fake, monkeypatch, db):
    from jobpilot.services import scheduler

    settings_store.put(db, "scheduler", {"tasks": {"collect_jobs": {"enabled": True, "every_hours": 24},
                                                   "notify": {"enabled": True, "every_hours": 0.25}}})
    db.commit()

    def broken(*a, **k):
        raise RuntimeError("telegram exploded")

    monkeypatch.setattr(notifications, "run", broken)
    res = scheduler.run_due(tg, app.state.sessionmaker)
    assert res == {"collect_jobs": "success", "notify": "failed"}
    with app.state.sessionmaker() as s:
        retry_at = scheduler.backoff_until(s, "notify", 0.25)
    assert retry_at is not None, "a failed task backs off instead of retrying every pass"


def test_scheduler_rejects_sub_hourly_network_tasks():
    with pytest.raises(Exception, match="at least 1"):
        settings_store.validate("scheduler", {"tasks": {"collect_jobs": {"enabled": True, "every_hours": 0.25}}})
    settings_store.validate("scheduler", {"tasks": {"notify": {"enabled": True, "every_hours": 0.1}}})


# ---- Dashboard, CLI, migration ---------------------------------------------------------------------------------

def test_dashboard_shows_status_without_secrets(tg, fake, monkeypatch):
    from fastapi.testclient import TestClient

    from jobpilot.web.app import create_app

    app = create_app(tg)
    c = TestClient(app, follow_redirects=False)
    for path in ("/telegram", "/", "/settings", "/system", "/health"):
        r = c.get(path)
        assert r.status_code == 200 and TOKEN not in r.text and "AAFakeToken" not in r.text, path
    assert "Telegram notifications" in c.get("/telegram").text
    assert "configured" in c.get("/").text
    monkeypatch.setattr(telegram.requests, "Session", lambda: fake)
    r = c.post("/telegram/test")
    assert "confirmed+delivery" in r.headers["location"] and len(fake.sent()) == 1
    fake.queue["sendMessage"] = [(401, {"ok": False, "description": "Unauthorized"})]
    r = c.post("/telegram/test")
    assert "err=" in r.headers["location"] and TOKEN not in r.headers["location"]
    app.state.engine.dispose()


def test_cli_status_and_set_chat_never_overwrite(tmp_path, monkeypatch, capsys):
    from jobpilot import cli
    from jobpilot.config import get_settings

    env = tmp_path / "jp.env"
    env.write_text(f"JOBPILOT_DATA_DIR={tmp_path / 'data'}\nJOBPILOT_TELEGRAM_ENABLED=true\n"
                   f"JOBPILOT_TELEGRAM_BOT_TOKEN={TOKEN}\n")
    monkeypatch.setenv("JOBPILOT_ENV_FILE", str(env))
    for var in ("JOBPILOT_DATA_DIR", "JOBPILOT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    fake = FakeTelegram()
    fake.updates = [msg(5, chat="777", text_="/start")]
    monkeypatch.setattr(telegram.requests, "Session", lambda: fake)
    try:
        assert cli.main(["init"]) == 0
        capsys.readouterr()
        assert cli.main(["telegram", "status"]) == 0
        out = capsys.readouterr().out
        assert TOKEN not in out and json.loads(out)["chat_id_set"] is False
        assert cli.main(["telegram", "find-chat"]) == 0
        assert '"chat_id": "777"' in capsys.readouterr().out
        assert cli.main(["telegram", "set-chat", "555"]) == 2, "an id that never messaged the bot is not accepted"
        assert cli.main(["telegram", "set-chat", "777"]) == 0
        content = env.read_text()
        assert content.endswith("JOBPILOT_TELEGRAM_CHAT_ID=777\n") and content.count(TOKEN) == 1, "only appended"
        assert cli.main(["telegram", "set-chat", "888", "--force"]) == 1, "an existing value is never overwritten"
        assert "JOBPILOT_TELEGRAM_CHAT_ID=888" not in env.read_text()
        assert TOKEN not in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_migration_0010_up_and_down(tmp_path):
    from alembic import command

    from jobpilot.config import Settings
    from jobpilot.db import alembic_config, make_engine, migrate

    s = Settings(_env_file=None, data_dir=tmp_path / "d")
    s.ensure_dirs()
    eng = make_engine(s)
    cfg = alembic_config(s)
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0009")
        conn.execute(text("INSERT INTO app_settings (key, value, updated_at) VALUES ('matching', '{}', '2026-01-01')"))
    assert migrate(s, eng)["backup"]
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0009")
    with eng.connect() as conn:
        tables = {r[0] for r in conn.execute(text("select name from sqlite_master where type='table'"))}
        assert not tables & {"notifications", "notification_keys", "telegram_updates", "integration_state"}
        assert conn.execute(text("select count(*) from app_settings")).scalar() == 1
    eng.dispose()


def test_dry_run_collection_never_counts_as_a_collection(app, settings, monkeypatch, capsys):
    from jobpilot import cli
    from jobpilot.config import get_settings
    from jobpilot.services import scheduler

    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(settings.data_dir))
    get_settings.cache_clear()
    try:
        assert cli.main(["collect", "--dry-run"]) == 0
    finally:
        get_settings.cache_clear()
    with app.state.sessionmaker() as s:
        assert scheduler.last_success(s, "collect_jobs") is None, "a dry run must not postpone the real collection"
        assert scheduler.last_success(s, "collect_jobs_dry_run") is not None
        assert "Last successful job collection: never" in notifications.daily_summary(s, settings)
