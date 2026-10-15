"""Structured logging: JSON lines to a rotating file, readable text to stderr."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path

from jobpilot.config import Settings

LOG_FILE_NAME = "jobpilot.log"
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except TypeError:
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured_for: Path | None = None


def configure_logging(settings: Settings, console_level: str | None = None) -> Path:
    """Configure root logging once per log directory. Returns the log file path.

    The log file always receives ``settings.log_level``; the console can be quieter
    (CLI commands pass ``"WARNING"`` so routine runs only print their own output).
    """
    global _configured_for
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_dir / LOG_FILE_NAME
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    if _configured_for == log_file:
        for h in root.handlers:
            if getattr(h, "_jobpilot_console", False):
                h.setLevel(console_level or settings.log_level)
        return log_file
    for h in list(root.handlers):
        if getattr(h, "_jobpilot", False):
            root.removeHandler(h)
            h.close()
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh._jobpilot = True  # type: ignore[attr-defined]
    sh = logging.StreamHandler()
    sh.setLevel(console_level or settings.log_level)
    sh._jobpilot_console = True  # type: ignore[attr-defined]
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    sh._jobpilot = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("urllib3", "httpx", "httpcore", "multipart", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured_for = log_file
    return log_file


def tail_log(settings: Settings, limit: int = 200, level: str | None = None) -> list[dict]:
    """Return the most recent structured log entries (newest first)."""
    log_file = settings.log_dir / LOG_FILE_NAME
    if not log_file.exists():
        return []
    with log_file.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 512_000))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    out: list[dict] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if level and entry.get("level") != level.upper():
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out
