"""Shared fixtures. Every test gets an isolated temporary data directory.

All job/profile data used in tests is SYNTHETIC and clearly fictional.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobpilot.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for var in list(__import__("os").environ):
        if var.startswith("JOBPILOT_"):
            monkeypatch.delenv(var)
    return Settings(_env_file=None, data_dir=tmp_path / "data", http_min_interval_seconds=0.0,
                    http_max_retries=1, respect_robots_txt=False)


PUBLIC_TEST_IP = "93.184.215.14"  # any globally routable literal; never contacted (HTTP is faked)


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """No real DNS in tests. The SSRF guard stays ON: hostnames resolve to a public test IP,
    except ``localhost`` / ``*.internal`` which resolve to loopback; IP literals map to themselves."""
    import ipaddress
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = "127.0.0.1" if host == "localhost" or host.endswith(".internal") else PUBLIC_TEST_IP
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr("jobpilot.sources.http.socket.getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def app(settings):
    from jobpilot.web.app import create_app

    application = create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def db(app):
    session = app.state.sessionmaker()
    yield session
    session.close()
