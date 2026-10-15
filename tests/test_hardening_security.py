"""Stage 7 security regression tests: SSRF/redirects, streaming limits, file permissions,
API docs exposure, backup handling. All offline (fake HTTP session, fake DNS)."""

from __future__ import annotations

import stat
import zipfile

import pytest

from jobpilot.sources import http as http_mod
from jobpilot.sources.http import BlockedByRobots, DisallowedAddress, FetchError
from tests.helpers import make_http

PUB = "https://careers.example.com"


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---- SSRF and redirects ----------------------------------------------------------------

@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8765/system/export.json", "http://localhost/", "http://10.1.2.3/", "http://192.168.0.10/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/",
    "http://0.0.0.0/", "http://224.0.0.1/", "http://metadata.internal/",
])
def test_private_destinations_rejected(settings, target):
    http = make_http(settings, {target: (200, "secret")})
    with pytest.raises(DisallowedAddress):
        http.get(target)
    assert http.session.calls == [], "nothing may be contacted"


@pytest.mark.parametrize("location", ["http://127.0.0.1:8765/system/export.json", "http://169.254.169.254/",
                                      "http://[::1]/x", "http://metadata.internal/x", "file:///etc/passwd"])
def test_redirect_to_disallowed_destination_rejected(settings, location):
    http = make_http(settings, {f"{PUB}/job/1": (302, "", {"Location": location}), location: (200, "secret")})
    with pytest.raises(FetchError):
        http.get(f"{PUB}/job/1")
    assert http.session.calls == [f"{PUB}/job/1"], "the redirect target must never be requested"


def test_valid_redirect_chain_followed_and_relative_location(settings):
    http = make_http(settings, {
        f"{PUB}/a": (301, "", {"Location": "/b"}),
        f"{PUB}/b": (302, "", {"Location": "https://jobs.example.org/c"}),
        "https://jobs.example.org/c": (200, "<h1>ok</h1>"),
    })
    r = http.get(f"{PUB}/a")
    assert r.text == "<h1>ok</h1>"
    assert http.session.calls == [f"{PUB}/a", f"{PUB}/b", "https://jobs.example.org/c"]


def test_redirect_loop_bounded(settings):
    http = make_http(settings, {f"{PUB}/loop": (302, "", {"Location": "/loop"})})
    with pytest.raises(FetchError, match="Too many redirects"):
        http.get(f"{PUB}/loop")
    assert len(http.session.calls) == http_mod.MAX_REDIRECTS + 1


def test_credentials_and_schemes_rejected(settings):
    http = make_http(settings, {})
    for url in ("https://user:pass@careers.example.com/", "ftp://careers.example.com/x", "file:///etc/passwd",
                "javascript:alert(1)", "https:///nohost"):
        with pytest.raises(FetchError):
            http.get(url)
    assert http.session.calls == []


def test_mixed_resolution_rejected(settings, monkeypatch):
    """A name resolving to both a public and a private address (DNS trick) is refused."""
    import socket

    monkeypatch.setattr("jobpilot.sources.http.socket.getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))])
    http = make_http(settings, {f"{PUB}/x": (200, "x")})
    with pytest.raises(DisallowedAddress):
        http.get(f"{PUB}/x")


# ---- Streaming limits -------------------------------------------------------------------

def test_declared_oversize_rejected_before_body(settings):
    http = make_http(settings, {f"{PUB}/big": (200, "small", {"Content-Length": str(http_mod.MAX_BYTES + 1)})})
    with pytest.raises(FetchError, match="too large"):
        http.get(f"{PUB}/big")


def test_streamed_oversize_rejected(settings, monkeypatch):
    monkeypatch.setattr(http_mod, "MAX_BYTES", 1000)
    http = make_http(settings, {f"{PUB}/big": (200, "x" * 5000)})  # no Content-Length header
    with pytest.raises(FetchError, match="exceeded"):
        http.get(f"{PUB}/big", max_bytes=http_mod.MAX_BYTES)
    assert http.get(f"{PUB}/big", max_bytes=10_000).text == "x" * 5000


def test_robots_size_capped_and_validated(settings, monkeypatch):
    settings.respect_robots_txt = True
    monkeypatch.setattr(http_mod, "ROBOTS_MAX_BYTES", 100)
    http = make_http(settings, {f"{PUB}/robots.txt": (200, "#" * 500), f"{PUB}/job": (200, "ok")})
    with pytest.raises(BlockedByRobots):
        http.get(f"{PUB}/job")  # oversized robots.txt -> conservative: don't crawl
    http2 = make_http(settings, {f"{PUB}/robots.txt": (302, "", {"Location": "http://127.0.0.1/robots.txt"}),
                                 f"{PUB}/job": (200, "ok")})
    with pytest.raises(DisallowedAddress):
        http2.get(f"{PUB}/job")
    http3 = make_http(settings, {f"{PUB}/robots.txt": (200, "User-agent: *\nAllow: /"), f"{PUB}/job": (200, "ok")})
    assert http3.get(f"{PUB}/job").text == "ok"


# ---- Permissions ---------------------------------------------------------------------------

def test_data_files_are_private(app, settings, db):
    from jobpilot.services import audit, backup

    audit.record(db, "perm.check")
    db.commit()
    path = backup.create_backup(settings, app.state.engine, label="perm")
    for d in (settings.data_path, settings.log_dir, settings.files_dir, settings.backup_dir, settings.secrets_dir):
        assert _mode(d) == 0o700, (d, oct(_mode(d)))
    for f in (settings.db_file, settings.log_dir / "jobpilot.log", path):
        assert _mode(f) & 0o077 == 0, (f, oct(_mode(f)))


def test_existing_permissive_data_dir_is_tightened(tmp_path):
    from jobpilot.config import Settings
    from jobpilot.web.app import create_app

    data = tmp_path / "loose"
    (data / "logs").mkdir(parents=True)
    data.chmod(0o775)
    (data / "logs").chmod(0o775)
    (data / "logs" / "jobpilot.log").write_text("")
    (data / "logs" / "jobpilot.log").chmod(0o664)
    s = Settings(_env_file=None, data_dir=data)
    application = create_app(s)
    application.state.engine.dispose()
    assert _mode(data) == 0o700 and _mode(data / "logs") == 0o700
    assert _mode(data / "logs" / "jobpilot.log") & 0o077 == 0
    assert _mode(s.db_file) & 0o077 == 0


# ---- API docs -------------------------------------------------------------------------------

def test_api_docs_disabled_by_default(client):
    for url in ("/api/docs", "/docs", "/redoc", "/openapi.json"):
        assert client.get(url).status_code == 404, url


def test_openapi_schema_opt_in_without_cdn(tmp_path):
    from fastapi.testclient import TestClient

    from jobpilot.config import Settings
    from jobpilot.web.app import create_app

    application = create_app(Settings(_env_file=None, data_dir=tmp_path / "d", enable_openapi=True))
    c = TestClient(application)
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/api/docs").status_code == 404, "no Swagger UI (it would load a third-party CDN)"
    application.state.engine.dispose()


# ---- Backups ---------------------------------------------------------------------------------

def test_restore_rejects_oversized_archive(settings, monkeypatch):
    from jobpilot.services import backup

    settings.ensure_dirs()
    monkeypatch.setattr(backup, "MAX_RESTORE_BYTES", 10_000)
    bomb = settings.backup_dir / "jobpilot-bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("jobpilot.db", b"\0" * 50_000)
        zf.writestr("manifest.json", "{}")
    with pytest.raises(backup.BackupError, match="too large"):
        backup.restore_backup(settings, bomb)


def test_backup_excludes_secrets_and_browser_profile(app, settings):
    from jobpilot.services import backup

    settings.gmail_token_file.write_text('{"refresh_token": "synthetic"}')
    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    (settings.browser_profile_dir / "Cookies").write_text("synthetic")
    path = backup.create_backup(settings, app.state.engine)
    names = zipfile.ZipFile(path).namelist()
    assert not any("secret" in n or "token" in n or "browser_profile" in n or "Cookies" in n for n in names)
