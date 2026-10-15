"""Additional CLI commands (jobs, assist, scheduler, gmail, export)."""

from __future__ import annotations

import json
import sys


def cmd_collect(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.services import jobs as job_svc
    from jobpilot.services.tasks import run_task

    settings, _, factory = _ctx()
    ids = [int(x) for x in args.source] if args.source else None
    run = run_task(settings, factory, "collect_jobs", lambda s: job_svc.collect_all(s, settings, source_ids=ids),
                   trigger="manual", retries=1)
    print(json.dumps({"status": run.status, "message": run.message, "details": run.details}, indent=2, default=str))
    return 0 if run.status == "success" else 1


def cmd_import(args) -> int:
    from pathlib import Path

    from jobpilot.cli import _ctx
    from jobpilot.services import jobs as job_svc

    _, _, factory = _ctx()
    path = Path(args.file)
    with factory() as s:
        stats = job_svc.import_file(s, path.name, path.read_bytes())
        s.commit()
    stats.pop("job_ids", None)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_add_url(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.services import jobs as job_svc
    from jobpilot.sources.http import PoliteHttp

    settings, _, factory = _ctx()
    with factory() as s:
        job, outcome, warnings = job_svc.add_from_url(s, PoliteHttp(settings), args.url, args.company)
        s.commit()
        print(json.dumps({"job_id": job.id, "title": job.title, "outcome": outcome, "warnings": warnings,
                          "relevance": job.relevance_score, "eligibility": job.eligibility}, indent=2))
    return 0


def cmd_rematch(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.services import matching

    _, _, factory = _ctx()
    with factory() as s:
        n = matching.rematch_all(s)
        s.commit()
    print(f"Re-scored {n} job(s)")
    return 0


def cmd_assist(args) -> int:
    from jobpilot.automation.apply import run_assist
    from jobpilot.cli import _ctx
    from jobpilot.models import Application
    from jobpilot.services.tasks import TaskLocked, task_lock

    settings, _, factory = _ctx()
    try:
        with task_lock(settings, "browser"), factory() as s:
            app = s.get(Application, args.application_id)
            if app is None:
                print("Application not found", file=sys.stderr)
                return 1
            run = run_assist(s, settings, app, wait_for_user=not args.no_wait)
            print(json.dumps({"status": run.status, "message": run.message, "filled": run.filled_fields,
                              "needs_you": run.unknown_fields, "paused": run.pause_reasons}, indent=2))
    except TaskLocked:
        print("Another browser session is already running (only one at a time).", file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    s = sub.add_parser("collect", help="Collect jobs from enabled sources and score them")
    s.add_argument("--source", nargs="*", help="Only these source ids")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("import-jobs", help="Import jobs from a CSV or JSON file")
    s.add_argument("file")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("add-url", help="Add a job from a posting URL")
    s.add_argument("url")
    s.add_argument("--company")
    s.set_defaults(func=cmd_add_url)

    sub.add_parser("rematch", help="Re-score all active jobs").set_defaults(func=cmd_rematch)

    s = sub.add_parser("assist", help="Open a browser to help fill an application (never submits)")
    s.add_argument("application_id", type=int)
    s.add_argument("--no-wait", action="store_true", help="Close the browser right after filling")
    s.set_defaults(func=cmd_assist)

    from jobpilot import cli_ops

    cli_ops.register(sub)
