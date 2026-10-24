"""Shared fixtures for browser-assisted application tests: a fake employer form on 127.0.0.1, a confirmed profile
with an approved resume, synthetic jobs and a fake Telegram setup. No real employer or chat is contacted.

(These lived in the auto-apply tests before that feature was removed on 2026-10-23.)
"""
from __future__ import annotations

import http.server
import threading
import time

import pytest

from jobpilot.models import Job, utcnow
from jobpilot.services import resumes as rs
from tests.helpers import make_pdf_bytes, seed_profile

FORM = """<html><body><h1>Apply: Java Backend Engineer</h1>
<form method="post" action="/apply" enctype="multipart/form-data">
<label for=fn>First Name *</label><input id=fn name=first_name required>
<label for=ln>Last Name *</label><input id=ln name=last_name required>
<label for=em>Email *</label><input id=em name=email type=email required>
<label for=cv>Resume/CV *</label><input id=cv name=resume type=file required>
<label for=why>Why do you want to work here? *</label><textarea id=why name=why required></textarea>
<label for=java>Years of Java experience *</label><select id=java name=java required><option value="">Select</option>
<option value="a">0-1</option><option value="b">1-3</option><option value="c">3+</option></select>
EXTRA
<label><input type=checkbox name=newsletter> Send me job alerts</label>
<button type=submit>Submit application</button></form></body></html>"""
CONFIRM = "<html><body><h1>Thank you for applying!</h1><p>Your application has been received.</p></body></html>"
ERROR = ("<html><body><form><div class='error' role=alert>Email is invalid</div>"
         "<button type=submit>Submit application</button></form></body></html>")


class EmployerSite:
    """A fake employer form. ``mode``: confirm | error | hang | captcha. Counts real form submissions."""

    def __init__(self, mode="confirm", extra=""):
        self.mode, self.posts, self.fields = mode, 0, []
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = FORM.replace("EXTRA", extra)
                if site.mode == "captcha":
                    body = body.replace("<form", "<iframe src='https://www.google.com/recaptcha/api2/anchor'></iframe><form")
                self._send(body)

            def do_POST(self):  # noqa: N802
                site.posts += 1
                length = int(self.headers.get("Content-Length") or 0)
                site.fields.append(self.rfile.read(length))
                if site.mode == "hang":
                    time.sleep(6)
                self._send(CONFIRM if site.mode == "confirm" else ERROR if site.mode == "error" else "<html>...</html>")

            def _send(self, body):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/apply"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def world(db, settings, monkeypatch):
    """Confirmed profile, an approved resume and the answer bank; site_for treats 127.0.0.1 and greenhouse.io as
    Greenhouse."""
    from jobpilot.automation import apply as apply_mod
    from jobpilot.services import answers as answer_svc

    seed_profile(db)
    master = rs.create_resume(db, "Master resume", "master", True)
    v = rs.import_version(db, settings, master, "Candidate.pdf", make_pdf_bytes(["Test Candidate", "Java"]))
    rs.approve_version(db, v, acknowledged=True)
    answer_svc.save_entry(db, "Why do you want to work here?", "I enjoy building reliable backend services (synthetic).",
                          category="motivation", approved=True)
    settings.browser_enabled = True
    monkeypatch.setattr(apply_mod, "site_for",
                        lambda url: "greenhouse" if url and ("127.0.0.1" in url or "greenhouse.io" in url) else None)
    db.commit()
    return settings


def job(db, url, title="Java Backend Engineer", score=82.0, eligibility="eligible", category="java_backend", **kw):
    j = Job(title=title, company_name=kw.pop("company", "Examplecorp"), status="active", user_status="new",
            relevance_score=score, eligibility=eligibility, category=category, fingerprint=f"fp-{title}-{url}",
            apply_url=url, source_url=url, first_seen_at=utcnow(), last_seen_at=utcnow(), **kw)
    db.add(j)
    db.commit()
    return j


@pytest.fixture
def chrome():
    pytest.importorskip("playwright.sync_api")


TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs"


@pytest.fixture
def tg(world):
    from pydantic import SecretStr

    world.telegram_enabled, world.telegram_bot_token, world.telegram_chat_id = True, SecretStr(TOKEN), "4242"
    return world
