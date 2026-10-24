"""Static checks of the Docker deployment files (no Docker needed). The live build/run checks are in docs/DOCKER.md."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text()


def test_dockerignore_is_an_allow_list_that_excludes_secrets_and_data():
    lines = [ln.strip() for ln in read(".dockerignore").splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == "*", "everything is excluded unless explicitly allowed"
    allowed = {ln[1:].rstrip("/") for ln in lines if ln.startswith("!")}
    assert allowed == {"pyproject.toml", "README.md", "src", "docker/entrypoint.sh", "docker/requirements.lock"}
    for pattern in ("**/.env", "**/.env.*", "**/*.db", "**/secrets", "**/*token*.json", "**/*client_secret*.json"):
        assert pattern in lines


def test_dockerfile_runs_non_root_with_health_check_and_no_secrets():
    df = read("Dockerfile")
    assert re.search(r"^ARG PYTHON_IMAGE=python:3\.12\.\d+-slim-\w+@sha256:[0-9a-f]{64}$", df, re.M), "pinned base"
    assert re.search(r"^USER jobpilot$", df, re.M)
    assert "http://127.0.0.1:8765/health" in df and "HEALTHCHECK" in df
    assert 'ENTRYPOINT ["jobpilot-entrypoint"]' in df and 'CMD ["serve"]' in df
    assert "STOPSIGNAL SIGTERM" in df and "JOBPILOT_DATA_DIR=/data" in df
    assert not re.search(r"(?i)(ARG|ENV)\s+\S*(TOKEN|SECRET|PASSWORD)", df), "no secrets in build args or layers"
    for forbidden in (".env", "data/", "secrets", ".venv", "playwright"):
        assert f"COPY {forbidden}" not in df


def test_compose_is_single_service_loopback_only_and_hardened():
    c = read("compose.yaml")
    assert c.count("image: jobpilot") == 1 and "services:" in c
    assert '"127.0.0.1:${JOBPILOT_HOST_PORT:-8765}:8765"' in c, "published on the host's loopback only"
    for line in ("read_only: true", "init: true", "restart: unless-stopped", "stop_grace_period: 90s",
                 "no-new-privileges:true", "- ALL", "/tmp:size=256m", "${JOBPILOT_DATA_SOURCE:-jobpilot-data}:/data"):
        assert line in c, line
    for absent in ("redis", "celery", "scheduler:", "worker:", "0.0.0.0:8765:"):
        assert absent not in c.lower(), absent


def test_env_example_has_placeholders_only_and_real_settings():
    from jobpilot.config import Settings

    text = read(".env.docker.example")
    pairs = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln and not ln.startswith("#"))
    fields = {f"JOBPILOT_{k.upper()}" for k in Settings.model_fields}
    unknown = [k for k in pairs if k.startswith("JOBPILOT_") and k not in fields]
    assert not unknown, f"variables the app does not read: {unknown}"
    assert pairs["JOBPILOT_TELEGRAM_BOT_TOKEN"] == "" and pairs["JOBPILOT_TELEGRAM_CHAT_ID"] == ""
    assert not re.search(r"\d{6,12}:[A-Za-z0-9_-]{30,}", text), "no token-shaped values"
    for fixed in ("JOBPILOT_DATA_DIR", "JOBPILOT_HOST", "JOBPILOT_PORT"):
        assert fixed not in pairs, f"{fixed} is set by compose.yaml, not the env file"


def test_lock_pins_every_runtime_dependency():
    lock = read("docker/requirements.lock")
    pins = dict(ln.split("==") for ln in lock.splitlines() if "==" in ln)
    for dep in ("fastapi", "uvicorn", "sqlalchemy", "alembic", "pydantic", "pydantic-settings", "jinja2",
                "python-multipart", "requests", "beautifulsoup4", "pypdf"):
        assert dep in pins, dep
    assert not {"pytest", "playwright", "httpx"} & set(pins), "no dev or browser extras in the image"


def test_entrypoint_starts_exactly_one_jobpilot_process():
    ep = read("docker/entrypoint.sh")
    assert ep.count("exec jobpilot") == 1
    assert not re.search(r"(?<!&)&\s*$", ep, re.M), "no command is started in the background"
    assert "jobpilot init" in ep and "set -eu" in ep


def test_secret_env_files_are_git_ignored():
    gi = read(".gitignore")
    assert ".env.*" in gi and "!.env.docker.example" in gi
