"""Real-browser regression test: form POSTs from the app's own pages must pass the CSRF check.

Regression for: "Cross-origin request blocked" when saving any form. `Referrer-Policy: no-referrer`
made Chrome send `Origin: null`, which the same-origin check rejects. TestClient doesn't emulate
browser Origin behaviour, so this needs a real (headless) Chrome against a live server.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from sqlalchemy import select


@pytest.fixture
def live_server(app):
    uvicorn = pytest.importorskip("uvicorn")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def chrome():
    pw_mod = pytest.importorskip("playwright.sync_api")
    pw = pw_mod.sync_playwright().start()
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pw.stop()
        pytest.skip(f"Chrome not available: {exc}")
    yield browser
    browser.close()
    pw.stop()


def test_saving_forms_in_a_real_browser_is_not_blocked(live_server, chrome, db):
    from jobpilot.models import CandidateProfile, Skill

    page = chrome.new_page()
    posts = []
    page.on("request", lambda r: posts.append(r.headers.get("origin")) if r.method == "POST" else None)

    page.goto(f"{live_server}/profile")
    page.fill("input[name=full_name]", "Test Candidate")
    with page.expect_navigation():
        page.click("text=Save details")
    assert "Cross-origin request blocked" not in page.content()
    assert "Profile saved" in page.inner_text("body")
    assert posts[-1] == live_server, f"browser sent Origin={posts[-1]!r}"

    page.goto(f"{live_server}/profile#skills")
    page.eval_on_selector("#skills details", "d => d.open = true")  # expand "Add skills" regardless of state
    page.fill("#skills input[name=name]", "Java")
    with page.expect_navigation():
        page.click("#skills button:has-text('Save')")
    assert "saved" in page.inner_text("body").lower()

    db.expire_all()
    assert db.scalar(select(CandidateProfile)).full_name == "Test Candidate"
    assert db.scalar(select(Skill)).normalized_name == "java"


def test_referrer_policy_keeps_origin_for_same_site_only(client):
    policy = client.get("/").headers["referrer-policy"]
    assert policy == "same-origin", "no-referrer makes browsers send Origin: null, breaking every form"


@pytest.mark.parametrize("url,expected", [
    ("/profile#skills", "/profile?msg=Saved#skills"),
    ("/jobs?q=x#top", "/jobs?q=x&msg=Saved#top"),
    ("/drafts", "/drafts?msg=Saved"),
])
def test_flash_message_goes_before_fragment(url, expected):
    from jobpilot.web.deps import redirect

    assert redirect(url, msg="Saved").headers["location"] == expected
