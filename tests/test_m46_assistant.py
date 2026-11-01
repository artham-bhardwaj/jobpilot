"""Milestone 46: the JobPilot connector for Claude Code (MCP) and Claude over Telegram (synthetic data; Claude is faked)."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest
from pydantic import SecretStr

from jobpilot import mcp_server as m
from jobpilot.services import assistant
from jobpilot.services import jobs as job_svc
from jobpilot.services import telegram_bot
from jobpilot.sources.base import JobData

POST = ("Priya Sharma\nTalent Acquisition at Examplecorp\nExamplecorp is hiring a Backend Engineer (Java).\n"
        "Send resumes to priya [at] examplecorp [dot] com")


@pytest.fixture
def tools(app, settings, monkeypatch):
    monkeypatch.setattr(m, "_settings", settings)
    monkeypatch.setattr(m, "_factory", app.state.sessionmaker)
    return m


def _job(db, title="Backend Engineer", company="Examplecorp"):
    job, _ = job_svc.upsert(db, None, JobData(title=title, company_name=company, location="Pune, India",
                                              description_text="Java, Spring Boot (synthetic)",
                                              source_url=f"https://example.com/{title.replace(' ', '-')}"), "manual")
    job.relevance_score = 80.0
    db.commit()
    return job


def test_search_details_and_status_tools(tools, db):
    job = _job(db)
    assert f"#{job.id} [80] Backend Engineer — Examplecorp" in tools.search_jobs("backend")
    assert "Examplecorp" in tools.job_details(job.id) and f"/jobs/{job.id}" in tools.job_details(job.id)
    assert "is now shortlisted" in tools.set_job_status(job.id, "shortlisted")
    assert "★" in tools.search_jobs(shortlisted_only=True)
    assert tools.job_details(999999) == "No job #999999."
    assert "Jobs: 1 active" in tools.overview()


def test_hiring_post_lead_and_draft_tools_never_send(tools, db):
    assert "priya@examplecorp.com" in tools.read_hiring_post(POST)
    out = tools.save_hiring_post_lead(POST, "Backend Engineer", "Examplecorp", email="priya@examplecorp.com",
                                      email_published_in_post=True, poster_name="Priya Sharma")
    assert "Drafted D" in out and "Not sent" in out
    did = int(out.split("Drafted D")[1].split(":")[0])
    assert "Hi Priya" in tools.read_draft(did)
    assert "updated" in tools.edit_draft(did, "Backend Engineer role", "Hi Priya,\n\nShort note.\n\nThanks")
    assert "Short note." in tools.read_draft(did)
    assert f"D{did} draft" in tools.list_drafts()
    # No tool can approve or send.
    assert not {"approve", "send", "approve_draft", "send_email", "submit"} & {f.__name__ for f in tools.TOOLS}


def test_expected_failures_come_back_as_text(tools):
    safe = m._safe(m.save_hiring_post_lead)
    out = safe(POST, "Backend Engineer", "Examplecorp", email="priya@examplecorp.com")  # not confirmed as published
    assert out.startswith("Not done:") and "published" in out
    assert "LinkedIn" in m.add_job_from_url("https://www.linkedin.com/jobs/view/1")


def test_server_registers_every_tool():
    pytest.importorskip("mcp")
    import asyncio

    server = m.build_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {f.__name__ for f in m.TOOLS}


# ---- Telegram bridge ------------------------------------------------------------------------------------------

def _tg(settings):
    settings.telegram_enabled, settings.telegram_bot_token, settings.telegram_chat_id = True, SecretStr("1:x"), "4242"


def _say(db, settings, text):
    upd = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 4242, "type": "private"}, "text": text}}
    return telegram_bot.handle_update(db, settings, upd)


def test_plain_message_goes_to_claude_only_when_the_assistant_runs(db, settings):
    _tg(settings)
    assert "only understand commands" in _say(db, settings, "find me java jobs")[2]
    assert not assistant.inbox(settings).exists() or not list(assistant.inbox(settings).glob("*.json"))
    (settings.data_path / "assistant").mkdir(parents=True, exist_ok=True)
    (settings.data_path / "assistant" / "heartbeat").touch()
    result, command, reply = _say(db, settings, "find me java jobs")
    assert (command, reply) == ("assistant", "🤖 Asking Claude…")
    [item] = list(assistant.inbox(settings).glob("*.json"))
    assert json.loads(item.read_text())["text"] == "find me java jobs"
    assert "new conversation" in _say(db, settings, "/new")[2]
    # A stale heartbeat means the worker is gone.
    old = time.time() - 600
    os.utime(settings.data_path / "assistant" / "heartbeat", (old, old))
    assert "only understand commands" in _say(db, settings, "hello")[2]


class FakeClaude:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, cmd, input, **kw):
        self.calls.append((cmd, input))
        code, out = self.answers.pop(0)
        lines = [{"type": "system"}, LIMIT_EVENT, {"type": "result", **out}]
        return subprocess.CompletedProcess(cmd, code, stdout="\n".join(map(json.dumps, lines)), stderr="")


LIMIT_EVENT = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": 0.1, "resetsAt": 1793554200, "status": "allowed"},
    "seven_day": {"utilization": 0.29, "resetsAt": 1793923200, "status": "allowed"}}}}


def test_worker_answers_continues_the_conversation_and_resets(settings, monkeypatch):
    monkeypatch.setattr(assistant, "claude_binary", lambda: "/usr/bin/claude")
    fake = FakeClaude((0, {"result": "You have **3** jobs.", "session_id": "s1"}),
                      (0, {"result": "#12", "session_id": "s1"}),
                      (0, {"result": "Fresh start", "session_id": "s2"}))
    sent = []
    state = assistant.handle_item(settings, {"text": "how many jobs?"}, {}, sent.append, run=fake)
    assert sent == ["You have 3 jobs."] and state["session_id"] == "s1"
    cmd, stdin = fake.calls[0]
    assert stdin == "how many jobs?" and "how many jobs?" not in cmd  # the message is never a command argument
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd and "--resume" not in cmd
    state = assistant.handle_item(settings, {"text": "which one?"}, state, sent.append, run=fake)
    assert fake.calls[1][0][fake.calls[1][0].index("--resume") + 1] == "s1"
    state = assistant.handle_item(settings, {"reset": True}, state, sent.append, run=fake)
    assert state == {}
    assistant.handle_item(settings, {"text": "hi"}, state, sent.append, run=fake)
    assert "--resume" not in fake.calls[2][0] and sent[-1] == "Fresh start"


def test_worker_reports_errors_and_retries_a_lost_conversation(settings, monkeypatch):
    monkeypatch.setattr(assistant, "claude_binary", lambda: "/usr/bin/claude")
    fake = FakeClaude((1, {"is_error": True, "result": "No conversation found"}), (0, {"result": "ok", "session_id": "s9"}))
    sent = []
    state = assistant.handle_item(settings, {"text": "hi"}, {"session_id": "gone", "last_used": time.time()},
                                  sent.append, run=fake)
    assert sent == ["ok"] and state["session_id"] == "s9"
    fake = FakeClaude((1, {"is_error": True, "result": "usage limit reached"}))
    assistant.handle_item(settings, {"text": "hi"}, {}, sent.append, run=fake)
    assert sent[-1].startswith("⚠️ Claude couldn't answer: usage limit reached")


def test_run_worker_takes_each_message_once(settings, monkeypatch):
    monkeypatch.setattr(assistant, "claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(assistant, "ask_claude", lambda s, text, sid, run=None: {"reply": f"re: {text}", "session_id": "s", "error": None})
    assistant.enqueue(settings, "one")
    assistant.enqueue(settings, "two")
    sent = []
    assistant.run_worker(settings, once=True, send=sent.append)
    assert sent == ["re: one", "re: two"] and not list(assistant.inbox(settings).glob("*.json"))
    assert assistant.worker_alive(settings)
    assistant.run_worker(settings, once=True, send=sent.append)
    assert len(sent) == 2


def test_mcp_config_points_at_this_data_dir(settings):
    cfg = assistant.mcp_config(settings)
    assert cfg["args"] == ["mcp"] and cfg["env"]["JOBPILOT_DATA_DIR"] == str(settings.data_path)
    assert "ExecStart=" in assistant.service_unit(settings.data_path) and "assistant run" in assistant.service_unit(settings.data_path)


def test_usage_is_recorded_and_reported(settings, monkeypatch, db):
    monkeypatch.setattr(assistant, "claude_binary", lambda: "/usr/bin/claude")
    assert "not known yet" in assistant.usage_report(settings)
    fake = FakeClaude((0, {"result": "hi", "session_id": "s1", "num_turns": 2, "duration_ms": 6000,
                           "total_cost_usd": 0.031, "usage": {"input_tokens": 100, "cache_read_input_tokens": 9000,
                                                              "output_tokens": 400}, "modelUsage": {"claude-sonnet-5-5": {}}}))
    assistant.handle_item(settings, {"text": "hello"}, {}, lambda t: None, run=fake)
    rep = assistant.usage_report(settings)
    assert "5-hour limit: 10% used" in rep and "Weekly limit: 29% used" in rep
    assert "Today: 1 question(s), 10k tokens, ~6 s each ($0.03 if it were paid API use; not charged)" in rep
    # /usage with the helper running: it queues a refresh and the helper replies with fresh numbers.
    _tg(settings)
    assert "isn't running, so these may be out of date" in _say(db, settings, "/usage")[2]
    (settings.data_path / "assistant" / "heartbeat").touch()
    assert _say(db, settings, "/usage")[2] == "📊 Checking your Claude usage…"
    item = json.loads(sorted(assistant.inbox(settings).glob("*.json"))[-1].read_text())
    sent = []
    probe = FakeClaude((0, {"result": "ok"}))
    assistant.handle_item(settings, item, {}, sent.append, run=probe)
    assert sent and sent[0].startswith("📊 Claude usage") and "--tools" in probe.calls[0][0]
    assert "Today: 1 question(s)" in sent[0]  # the refresh itself is not counted as a question


# ---- tailored resumes from chat ---------------------------------------------------------------------------------

from tests.test_m34_tailored import needs_tex, tworld  # noqa: E402,F401


class FakeTelegram:
    sent: list = []

    def __init__(self, settings):
        pass

    def send_document(self, data, filename, caption=""):
        FakeTelegram.sent.append((filename, data, caption))
        return 1


@needs_tex
def test_resume_is_made_and_sent_as_pdf_and_latex(tworld, tools, monkeypatch):
    from jobpilot.integrations import telegram
    from jobpilot.services import tailored

    monkeypatch.setattr(telegram, "TelegramClient", FakeTelegram)
    monkeypatch.setattr(telegram, "is_ready", lambda s: True)
    FakeTelegram.sent = []
    out = tools.create_tailored_resume(tworld.job.id)
    assert out.startswith("Resume R") and "validated" in out and "java" in out, out
    rid = int(out.split("Resume R")[1].split()[0])
    assert tools.create_tailored_resume(tworld.job.id).startswith(f"Resume R{rid} "), "unchanged inputs: reused"
    assert tools.send_resume_to_telegram(rid, include_latex=True) == f"Sent the PDF and LaTeX source of R{rid} to Telegram."
    (pdf_name, pdf, caption), (tex_name, tex, _) = FakeTelegram.sent
    assert pdf_name.endswith(".pdf") and pdf.startswith(b"%PDF") and "Examplecorp" in caption
    assert tex_name.endswith(".tex") and b"\\begin{document}" in tex
    import hashlib

    from jobpilot.models.tailored import TailoredResume

    with tools._db() as s:
        tr = s.get(TailoredResume, rid)
        assert hashlib.sha256(tex).hexdigest() == tr.content_sha256, "the .tex is exactly what was compiled"
        assert hashlib.sha256(pdf).hexdigest() == tr.pdf_sha256
        # A changed PDF on disk is never sent.
        (tworld.settings.files_dir / tr.pdf_path).write_bytes(b"%PDF-1.4 tampered")
    assert "missing or changed" in tools.send_resume_to_telegram(rid)
    assert len(FakeTelegram.sent) == 2
    assert "not a valid tailored resume" in tools.send_resume_to_telegram(999)


def test_job_from_pasted_text_and_no_template(tools, db):
    out = tools.add_job_from_text("Backend Engineer", "Examplecorp", "Java, Spring Boot, Kafka (synthetic).")
    jid = int(out.split("#")[1].split()[0])
    assert "Backend Engineer — Examplecorp" in out
    # No Master resume registered yet: a clear message, nothing crashes.
    res = m._safe(tools.create_tailored_resume)(jid)
    assert "not made" in res or res.startswith("Not done:"), res


def test_telegram_send_document_uploads_multipart(settings):
    from jobpilot.integrations import telegram

    _tg(settings)
    calls = []

    class Http:
        def post(self, url, **kw):
            calls.append((url, kw))
            return type("R", (), {"status_code": 200, "json": lambda self: {"ok": True, "result": {"message_id": 7}}})()

    client = telegram.TelegramClient(settings, http=Http())
    assert client.send_document(b"%PDF-1.4 x", "Resume.pdf", "caption") == 7
    url, kw = calls[0]
    assert url.endswith("/sendDocument") and kw["files"] == {"document": ("Resume.pdf", b"%PDF-1.4 x")}
    assert kw["data"] == {"chat_id": "4242", "caption": "caption"} and "json" not in kw
