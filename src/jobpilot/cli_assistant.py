"""``jobpilot mcp`` (the connector Claude Code talks to) and ``jobpilot assistant …`` (Claude over Telegram)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from jobpilot.cli import CliError


def cmd_mcp(args) -> int:
    try:
        from jobpilot import mcp_server
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
        raise CliError("The connector needs the 'mcp' package: pip install -e '.[assistant]'", code=2) from exc
    mcp_server.main()
    return 0


def _settings():
    from jobpilot.cli import _ctx

    settings, _, _ = _ctx(run_migrations=False)
    return settings


def cmd_assistant(args) -> int:
    from jobpilot.integrations import telegram
    from jobpilot.services import assistant

    settings = _settings()
    if args.action == "run":
        if not telegram.is_ready(settings):
            raise CliError("Telegram is not configured in this environment (.env): the assistant needs the bot "
                           "token and chat id to reply.", code=2)
        if assistant.claude_binary() is None:
            raise CliError("Claude Code (`claude`) was not found. Install it and sign in first.", code=2)
        print("JobPilot assistant: answering plain Telegram messages with Claude. Ctrl+C to stop.", flush=True)
        assistant.run_worker(settings)
        return 0
    if args.action == "status":
        hb = assistant._dir(settings) / "heartbeat"
        alive = assistant.worker_alive(settings)
        waiting = len(list(assistant.inbox(settings).glob("*.json"))) if assistant.inbox(settings).exists() else 0
        print(f"Assistant: {'running' if alive else 'not running'}"
              + (f" (last seen {int(time.time() - hb.stat().st_mtime)} s ago)" if hb.exists() else "")
              + f"; {waiting} message(s) waiting; Claude Code: {assistant.claude_binary() or 'NOT FOUND'}")
        return 0
    if args.action == "setup":
        cfg = assistant.mcp_config(settings)
        claude = assistant.claude_binary()
        if claude is None:
            raise CliError("Claude Code (`claude`) was not found. Install it and sign in first.", code=2)
        subprocess.run([claude, "mcp", "remove", "-s", "user", "jobpilot"], capture_output=True)
        r = subprocess.run([claude, "mcp", "add-json", "-s", "user", "jobpilot", json.dumps(cfg)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise CliError(f"Registering the connector with Claude Code failed: {(r.stderr or r.stdout).strip()}")
        print("Connector registered with Claude Code (user scope, name 'jobpilot'). In Claude Code, run /mcp to see it.")
        unit = Path.home() / ".config" / "systemd" / "user" / "jobpilot-assistant.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(assistant.service_unit(Path.cwd()))
        print(f"Service file written: {unit}\n  Start it now and at login:  systemctl --user daemon-reload && "
              "systemctl --user enable --now jobpilot-assistant")
        return 0
    raise CliError(f"unknown action {args.action}")  # pragma: no cover


def register(sub) -> None:
    s = sub.add_parser("mcp", help="Run the JobPilot connector for Claude Code (MCP over stdio)")
    s.set_defaults(func=cmd_mcp)
    s = sub.add_parser("assistant", help="Claude over Telegram: run the helper, check it, or set it up")
    s.add_argument("action", choices=["run", "status", "setup"],
                   help="run: answer plain Telegram messages with Claude Code; status: is it running; "
                        "setup: register the connector with Claude Code and write a systemd user service")
    s.set_defaults(func=cmd_assistant)
