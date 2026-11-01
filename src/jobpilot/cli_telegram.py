"""CLI for Telegram: ``jobpilot telegram status|check|find-chat|set-chat|test|notify|summary|poll``.

Nothing here prints the bot token. ``set-chat`` only appends one line to .env; it never rewrites the file.
"""

from __future__ import annotations

import json
import sys


BOT_COMMANDS = [("/menu", "Buttons for everything"), ("/status", "System health and schedules"),
                ("/jobs", "Top jobs with eligibility"), ("/alljobs", "Every relevant job with its apply link"),
                ("/track", "Did you apply? Tell me with buttons"), ("/applied", "Your applications by company"),
                ("/company", "One company: /company amazon"), ("/dream", "Jobs at your dream companies"),
                ("/questions", "Questions waiting for your answer"),
                ("/outcomes", "Application status changes"), ("/applications", "Applications by status"),
                ("/recruiters", "Recruiter contacts"), ("/outreach", "Recruiter emails"),
                ("/outreach_pending", "Emails waiting for you"), ("/today", "Today's summary"),
                ("/pause", "Pause automatic recruiter outreach"), ("/resume", "Restart it"),
                ("/usage", "Claude plan limits and assistant use"), ("/new", "New conversation with Claude"),
                ("/help", "All commands")]


def _out(data) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _append_env(key: str, value: str) -> str:
    """Add ``key=value`` to the .env file in use; never overwrite an existing different value."""
    from pathlib import Path

    from jobpilot.config import env_file_path

    path = env_file_path() or Path(".env")
    lines = path.read_text().splitlines() if path.exists() else []
    for ln in lines:
        if ln.split("=", 1)[0].strip() == key:
            current = ln.split("=", 1)[1].strip() if "=" in ln else ""
            if current == value:
                return f"{key} is already set to this value in {path}"
            raise ValueError(f"{key} is already set to a different value in {path}; edit that line yourself if you "
                             "really want to change it")
    existed = path.exists()
    with path.open("a", encoding="utf-8") as f:
        if lines and lines[-1].strip():
            f.write("\n")
        f.write(f"{key}={value}\n")
    if not existed:
        path.chmod(0o600)
    return f"Added {key} to {path}. Restart `jobpilot serve` so it is picked up."


def cmd_telegram(args) -> int:
    from jobpilot.cli import CliError, _ctx
    from jobpilot.config import get_settings
    from jobpilot.integrations import telegram

    settings, _, factory = _ctx()
    try:
        if args.action == "status":
            _out(telegram.config_status(settings))
            return 0
        if not settings.telegram_bot_token:
            raise CliError("JOBPILOT_TELEGRAM_BOT_TOKEN is not set in .env", code=2)
        client = telegram.TelegramClient(settings)
        if args.action == "check":  # read-only: confirms the token is valid
            me = client.get_me()
            _out({"ok": True, "bot_username": me.get("username"), "bot_name": me.get("first_name"),
                  **telegram.config_status(settings)})
        elif args.action == "find-chat":
            updates = client.get_updates(None, poll_timeout=0)  # no offset: nothing is marked as read
            chats = {}
            for u in updates:
                chat = (u.get("message") or {}).get("chat") or {}
                if chat.get("id") is not None:
                    chats[str(chat["id"])] = {"chat_id": str(chat["id"]), "type": chat.get("type"),
                                              "username": chat.get("username"), "first_name": chat.get("first_name")}
            me = client.get_me()
            if not chats:
                print(f"No messages yet. In Telegram, open @{me.get('username')} and press Start (or send any message), "
                      "then run this command again.")
                return 1
            _out(list(chats.values()))
            print("\nIf one of these is you, configure it explicitly:\n  jobpilot telegram set-chat <chat_id>\n"
                  "Only that chat will receive notifications or get answers to commands.", file=sys.stderr)
        elif args.action == "set-chat":
            if not args.chat_id or not args.chat_id.lstrip("-").isdigit():
                raise CliError("Give the numeric chat id: jobpilot telegram set-chat <chat_id>", code=2)
            seen = {str(((u.get("message") or {}).get("chat") or {}).get("id")) for u in client.get_updates(None, 0)}
            if args.chat_id not in seen and not args.force:
                raise CliError("That chat id has not messaged the bot recently. Press Start in the bot chat first, "
                               "or pass --force if you are sure.", code=2)
            print(_append_env("JOBPILOT_TELEGRAM_CHAT_ID", args.chat_id))
        elif args.action == "test":
            if not settings.telegram_chat_id:
                raise CliError("JOBPILOT_TELEGRAM_CHAT_ID is not set: run `jobpilot telegram find-chat` first", code=2)
            from jobpilot.models import utcnow

            from jobpilot.services import telegram_ui

            ids = client.send_text(f"✅ JobPilot test message\nSent {utcnow():%Y-%m-%d %H:%M} UTC. Telegram "
                                   "notifications are working. No action needed.", reply_markup=telegram_ui.menu(),
                                   html=True)
            _out({"sent": True, "telegram_message_ids": ids, "chat_id": settings.telegram_chat_id})
        elif args.action == "menu":
            from jobpilot.services import telegram_ui

            client.call("setMyCommands", {"commands": [{"command": c.lstrip("/"), "description": d} for c, d in BOT_COMMANDS]})
            ids = client.send_text("⌨️ JobPilot keyboard is on\nThe common commands are now under the text box.",
                                   reply_markup=telegram_ui.reply_keyboard(), html=True)
            ids += client.send_text("🧭 JobPilot\nTap a button. Everything is read-only except Pause / Resume and the "
                                    "buttons on a batch.", reply_markup=telegram_ui.menu(), html=True)
            _out({"commands_registered": len(BOT_COMMANDS), "telegram_message_ids": ids})
        elif args.action == "summary":  # print only; never sends
            from jobpilot.services import notifications

            with factory() as s:
                print(notifications.daily_summary(s, settings))
        elif args.action == "notify":
            from jobpilot.services import notifications
            from jobpilot.services.tasks import run_task

            if args.dry_run:
                with factory() as s:
                    res = notifications.scan(s, settings)
                    texts = [n.text for n in notifications.recent(s, res["created"])] if res["created"] else []
                    s.rollback()  # nothing is recorded or sent
                _out({"dry_run": True, **res, "messages": list(reversed(texts))})
                return 0
            run = run_task(settings, factory, "notify", lambda s: notifications.run(s, settings), trigger="manual")
            _out({"status": run.status, "message": run.message})
            return 0 if run.status == "success" else 1
        elif args.action == "poll":
            from jobpilot.services import telegram_bot
            from jobpilot.services.tasks import TaskLocked

            print("Answering commands from your chat (Ctrl-C to stop)…" if not args.once else "", file=sys.stderr)
            try:
                while True:
                    try:
                        stats = telegram_bot.poll_once(settings, factory)
                    except TaskLocked as exc:
                        raise CliError("Another JobPilot process (probably `jobpilot serve`) is already answering "
                                       "Telegram commands.", code=2) from exc
                    if args.once:
                        _out(stats)
                        break
            except KeyboardInterrupt:
                pass
    except telegram.TelegramError as exc:
        print(f"Telegram: {telegram.redact(str(exc), get_settings())}", file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    s = sub.add_parser("telegram", help="Telegram notifications and read-only bot commands (see docs/TELEGRAM.md)")
    s.add_argument("action", choices=["status", "check", "find-chat", "set-chat", "test", "notify", "summary", "poll",
                                      "menu"],
                   help="status: configuration (no network); check: verify the token; find-chat: list chats that "
                        "messaged the bot; set-chat ID: add your chat id to .env; test: send one test message; "
                        "notify: scan and deliver now (--dry-run to preview); summary: print today's summary; "
                        "poll: answer commands in the foreground")
    s.add_argument("chat_id", nargs="?", help="set-chat: your numeric chat id")
    s.add_argument("--force", action="store_true", help="set-chat: accept an id that hasn't messaged the bot recently")
    s.add_argument("--dry-run", action="store_true", help="notify: show what would be sent; record and send nothing")
    s.add_argument("--once", action="store_true", help="poll: one long poll, then exit")
    s.set_defaults(func=cmd_telegram)
