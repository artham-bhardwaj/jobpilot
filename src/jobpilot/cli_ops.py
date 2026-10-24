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
            rep = scheduler.status_report(s, settings)
            print(json.dumps(_status_json(rep, s, scheduler), indent=2))
    elif args.action == "run":
        from jobpilot.services.tasks import run_task

        if args.task not in scheduler.TASKS:
            print(f"Unknown task; choose from {', '.join(scheduler.TASKS)}", file=sys.stderr)
            return 2
        run = run_task(settings, factory, args.task, scheduler.TASKS[args.task](settings), trigger="manual", retries=1)
        print(json.dumps({"status": run.status, "message": run.message}, indent=2))
    return 0


def _when(dt) -> str | None:
    """'2026-10-17 17:56:51 UTC (23:26 local)': stored times are naive UTC."""
    if dt is None:
        return None
    from datetime import UTC, datetime

    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    local = dt.replace(tzinfo=UTC).astimezone()
    return f"{dt:%Y-%m-%d %H:%M:%S} UTC ({local:%a %H:%M %Z})"


def _status_json(rep: dict, s, scheduler) -> dict:
    lp = rep["last_pass"] or {}
    return {
        "scheduler": rep["scheduler"],
        "last_pass": None if not lp else {"finished_at": _when(lp.get("finished_at")), "runner": lp.get("runner"),
                                          "ran": lp.get("ran") or "nothing due (empty pass)"},
        "next_pass_expected": _when(rep["next_pass"]) or "unknown (no running scheduler)",
        "pass_overdue": rep["pass_overdue"],
        "note": rep["note"],
        "due": [n for n, t in rep["tasks"].items() if t["due"]],
        "tasks": {n: {"enabled": t["enabled"], "every_hours": t["every_hours"],
                      "last_run": None if not t["last_run"] else
                      f"{t['last_run']['status']} ({t['last_run']['trigger']}) at {_when(t['last_run']['started_at'])}",
                      "last_success": _when(t["last_success"]), "due_now": t["due"],
                      "next_due": _when(t["next_due"]) if t["enabled"] else "disabled",
                      "expected_run": _when(t["expected_run"]),
                      **({"retrying_after_failure_at": _when(t["retry_after_failure_at"])}
                         if t["retry_after_failure_at"] else {})}
                  for n, t in rep["tasks"].items()},
        "last_success": {n: str(scheduler.last_success(s, n)) for n in scheduler.TASKS},
    }


def cmd_gmail(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.db import session_scope
    from jobpilot.integrations import gmail
    from jobpilot.models import EmailDraft

    settings, _, factory = _ctx()

    def out(data) -> None:
        print(json.dumps(data, indent=2, default=str))

    def load(s, draft_id):
        d = s.get(EmailDraft, draft_id) if draft_id else None
        if d is None:
            raise gmail.GmailError("Give an existing local draft id with --draft ID")
        return d

    def state(d) -> dict:
        return {"draft": d.id, "local_status": d.status, "approved_at": d.approved_at, "to": d.to_address,
                "subject": d.subject, "gmail_status": d.gmail_status, "gmail_draft_id": d.gmail_draft_id,
                "gmail_thread_id": d.gmail_thread_id, "gmail_message_id": d.gmail_message_id,
                "gmail_attachment": d.gmail_attachment, "gmail_error": d.gmail_error, "sent_at": d.sent_at,
                "sent_via": d.sent_via}

    try:
        if args.action == "auth":
            tok = gmail.authorize(settings, open_browser=not args.no_browser)
            print(f"Authorised as {tok.get('email')}. Token stored at {settings.gmail_token_file} (mode 600).")
        elif args.action == "status":
            out(gmail.status(settings))
        elif args.action == "revoke":
            gmail.revoke(settings)
            print("Token revoked and deleted.")
        elif args.action == "check":  # read-only: proves the token works against the real API
            client = gmail.GmailClient(settings)
            out({"ok": True, "email": client.profile_email(), **gmail.readiness(settings)})
        elif args.action == "sync":
            with session_scope(factory) as s:
                res = gmail.sync_replies(s, settings, gmail.GmailClient(settings))
            out(res)
            return 1 if res["errors"] else 0
        elif args.action == "test-draft":
            if not args.to:
                raise gmail.GmailError("--to is required (use an address you control or a reserved one such as "
                                       "jobpilot-test@example.com; nothing is sent either way)")
            with session_scope(factory) as s:
                d = _make_test_draft(s, args.to, args.resume_version)
                try:
                    res = gmail.create_gmail_draft(s, settings, d, gmail.GmailClient(settings))
                except gmail.GmailError as exc:
                    out(state(d))
                    raise exc
                out({"created": not res["reused"], **state(d)})
        elif args.action == "create-draft":
            with session_scope(factory) as s:
                d = load(s, args.draft)
                res = gmail.create_gmail_draft(s, settings, d, gmail.GmailClient(settings))
                out({"created": not res["reused"], **state(d)})
        elif args.action == "draft-status":
            with session_scope(factory) as s:
                d = load(s, args.draft)
                res = gmail.reconcile(s, settings, d, gmail.GmailClient(settings)) if args.refresh else None
                out({**state(d), **({"gmail_check": res} if res else {})})
        elif args.action == "inspect":
            with factory() as s:
                d = load(s, args.draft)
                if not d.gmail_draft_id:
                    raise gmail.GmailError(f"Draft #{d.id} has no Gmail draft id (gmail_status={d.gmail_status})")
                out(gmail.inspect_gmail_draft(gmail.GmailClient(settings), d.gmail_draft_id))
    except gmail.GmailError as exc:
        print(f"Gmail: {exc}", file=sys.stderr)
        return 1
    return 0


def _make_test_draft(s, to: str, resume_version_id: int | None):
    """A clearly labelled local test draft (kind gmail_test), approved so it can be put into Gmail Drafts."""
    from jobpilot.models import EmailDraft, ResumeVersion, utcnow
    from jobpilot.services import audit

    rv = s.get(ResumeVersion, resume_version_id) if resume_version_id else None
    if resume_version_id and rv is None:
        raise ValueError(f"Resume version #{resume_version_id} not found")
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    body = ("This is a JobPilot connection test created from the command line. It is a draft only and must not be "
            "sent.\n\n" + ("A resume file is attached to check attachments.\n" if rv else "No attachment.\n") +
            "Delete this draft from Gmail after checking it.")
    d = EmailDraft(kind="gmail_test", to_address=to.strip().lower(), subject=f"[JobPilot test] Gmail draft check {stamp} — do not send",
                   body=body, status="approved", approved_at=utcnow(), missing_fields=[],
                   resume_version_id=rv.id if rv else None)
    s.add(d)
    s.flush()
    audit.record(s, "draft.test_created", "email_draft", d.id)
    return d


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
    s.add_argument("action", choices=["auth", "status", "check", "revoke", "sync", "test-draft", "create-draft",
                                      "draft-status", "inspect"],
                   help="check: verify the token with Gmail (read-only); test-draft: create a labelled test draft in "
                        "Gmail Drafts (never sends); create-draft/draft-status/inspect: for --draft ID")
    s.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")
    s.add_argument("--draft", type=int, help="Local JobPilot draft id")
    s.add_argument("--to", help="test-draft: recipient shown in the Gmail draft (nothing is sent)")
    s.add_argument("--resume-version", type=int, help="test-draft: attach this resume version's stored file")
    s.add_argument("--refresh", action="store_true", help="draft-status: ask Gmail for the current state first")
    s.set_defaults(func=cmd_gmail)

    s = sub.add_parser("export", help="Export data (CSV per entity, or everything as JSON)")
    s.add_argument("kind", choices=["jobs", "applications", "contacts", "drafts", "interviews", "sources", "skill_gaps",
                                   "summary", "all-json"])
    s.add_argument("--out")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("delete-data", help="Delete personal data (a backup is taken first unless backups are deleted too)")
    s.add_argument("scope", choices=["profile", "contacts", "outreach", "everything"])
    s.add_argument("--include-backups", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_delete)
