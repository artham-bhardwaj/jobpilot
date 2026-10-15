"""Operational CLI commands: scheduler, gmail, export, delete."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_scheduler(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.services import scheduler

    if args.action == "systemd":
        units = scheduler.systemd_units(sys.executable, str(Path.cwd()), args.every)
        out = Path(args.out) if args.out else None
        for name, text in units.items():
            if out:
                out.mkdir(parents=True, exist_ok=True)
                (out / name).write_text(text)
                print(f"wrote {out / name}")
            else:
                print(f"# ---- {name}\n{text}")
        print("\nInstall (user-level, no root):\n  mkdir -p ~/.config/systemd/user && cp "
              f"{out or '<files above>'}/jobpilot-scheduler.* ~/.config/systemd/user/\n"
              "  systemctl --user daemon-reload && systemctl --user enable --now jobpilot-scheduler.timer")
        return 0
    settings, _, factory = _ctx()
    if args.action == "run-due":
        print(json.dumps(scheduler.run_due(settings, factory, trigger="scheduled"), indent=2))
    elif args.action == "status":
        with factory() as s:
            print(json.dumps({"due": scheduler.due_tasks(s),
                              "last_success": {n: str(scheduler.last_success(s, n)) for n in scheduler.TASKS}}, indent=2))
    elif args.action == "run":
        from jobpilot.services.tasks import run_task

        if args.task not in scheduler.TASKS:
            print(f"Unknown task; choose from {', '.join(scheduler.TASKS)}", file=sys.stderr)
            return 2
        run = run_task(settings, factory, args.task, scheduler.TASKS[args.task](settings), trigger="manual", retries=1)
        print(json.dumps({"status": run.status, "message": run.message}, indent=2))
    return 0


def cmd_gmail(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.integrations import gmail

    settings, _, factory = _ctx()
    try:
        if args.action == "auth":
            tok = gmail.authorize(settings, open_browser=not args.no_browser)
            print(f"Authorised as {tok.get('email')}. Token stored at {settings.gmail_token_file} (mode 600).")
        elif args.action == "status":
            print(json.dumps(gmail.status(settings), indent=2))
        elif args.action == "revoke":
            gmail.revoke(settings)
            print("Token revoked and deleted.")
        elif args.action == "sync":
            with factory() as s:
                print(json.dumps(gmail.sync_replies(s, settings, gmail.GmailClient(settings)), indent=2))
                s.commit()
    except gmail.GmailError as exc:
        print(f"Gmail: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_export(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.services import analytics, privacy

    _, _, factory = _ctx()
    with factory() as s:
        if args.kind == "all-json":
            data = json.dumps(privacy.export_all(s), indent=1, default=str)
        else:
            data = analytics.export_csv(s, args.kind)
        s.commit()
    if args.out:
        Path(args.out).write_text(data)
        Path(args.out).chmod(0o600)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(data)
    return 0


def cmd_delete(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.services import backup, privacy

    settings, engine, factory = _ctx()
    if not args.yes:
        answer = input(f"Delete {args.scope} data{' and ALL backups' if args.include_backups else ''}? Type DELETE: ")
        if answer.strip() != "DELETE":
            print("Aborted.")
            return 1
    if not args.include_backups:
        print("Safety backup:", backup.create_backup(settings, engine, label=f"pre-delete-{args.scope}"))
    with factory() as s:
        counts = privacy.delete_data(s, settings, args.scope, include_backups=args.include_backups)
        s.commit()
    print(json.dumps(counts, indent=2))
    return 0


def register(sub) -> None:
    s = sub.add_parser("scheduler", help="Run due tasks, show status, or print systemd units")
    s.add_argument("action", choices=["run-due", "status", "run", "systemd"])
    s.add_argument("--task", help="Task name for 'run'")
    s.add_argument("--every", type=int, default=60, help="Timer interval in minutes (systemd)")
    s.add_argument("--out", help="Directory to write systemd unit files to")
    s.set_defaults(func=cmd_scheduler)

    s = sub.add_parser("gmail", help="Optional Gmail integration")
    s.add_argument("action", choices=["auth", "status", "revoke", "sync"])
    s.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")
    s.set_defaults(func=cmd_gmail)

    s = sub.add_parser("export", help="Export data (CSV per entity, or everything as JSON)")
    s.add_argument("kind", choices=["jobs", "applications", "contacts", "drafts", "interviews", "summary", "all-json"])
    s.add_argument("--out")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("delete-data", help="Delete personal data (a backup is taken first unless backups are deleted too)")
    s.add_argument("scope", choices=["profile", "contacts", "outreach", "everything"])
    s.add_argument("--include-backups", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_delete)
