"""Command-line interface: ``jobpilot <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jobpilot.config import env_file_path, get_settings


class CliError(Exception):
    """An expected, user-facing error: printed without a traceback."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _require_database(settings) -> None:
    if settings.db_file and not settings.db_file.exists():
        raise CliError(
            f"No JobPilot database at {settings.db_file}\n"
            "  Run `jobpilot init` to create one here, or point JOBPILOT_DATA_DIR (or JOBPILOT_ENV_FILE)\n"
            "  at your existing data directory. Relative paths resolve against the current directory.", code=2)


def _ctx(require_db: bool = True):
    from jobpilot.db import make_engine, make_sessionmaker, migrate
    from jobpilot.logging_setup import configure_logging

    settings = get_settings()
    if require_db:
        _require_database(settings)
    settings.ensure_dirs()
    configure_logging(settings, console_level="WARNING")
    engine = make_engine(settings)
    migrate(settings, engine)
    settings.secure_files()
    return settings, engine, make_sessionmaker(engine)


def cmd_init(args) -> int:
    settings, engine, factory = _ctx(require_db=False)
    from jobpilot.web.app import seed_reference_data

    seed_reference_data(engine)
    env = env_file_path()
    print(f"Config file:    {env.resolve() if env else '(none; using environment/defaults)'}")
    print(f"Data directory: {settings.data_path}")
    print(f"Database:       {settings.db_url}")
    from jobpilot.db import current_revision

    print(f"Schema:         {current_revision(engine)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    settings = get_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    try:
        settings.check_bind()
    except RuntimeError as exc:
        raise CliError(str(exc), code=2) from exc
    _require_database(settings)
    from jobpilot.logging_setup import configure_logging
    from jobpilot.web.app import create_app

    settings.ensure_dirs()
    configure_logging(settings, console_level="WARNING")
    app = create_app(settings)
    print(f"Data directory: {settings.data_path}", flush=True)
    if settings.scheduler_enabled:
        from jobpilot.services.scheduler import start_background_scheduler

        start_background_scheduler(settings, app.state.sessionmaker)
    print(f"JobPilot running at http://{settings.host}:{settings.port}", flush=True)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0


def cmd_db(args) -> int:
    settings, engine, _ = _ctx()
    from jobpilot.db import current_revision, head_revision

    print(json.dumps({"current": current_revision(engine), "head": head_revision(settings)}))
    return 0


def cmd_backup(args) -> int:
    from jobpilot.services import backup

    settings, engine, _ = _ctx()
    if args.action == "create":
        print(backup.create_backup(settings, engine, label=args.label or "manual"))
    elif args.action == "list":
        for b in backup.list_backups(settings):
            print(f"{b['name']}\t{b['size']}\t{b['modified']:%Y-%m-%d %H:%M}")
    elif args.action == "restore":
        if not args.name:
            print("--name is required", file=sys.stderr)
            return 2
        path = Path(args.name)
        path = path if path.is_file() else backup.resolve_backup(settings, args.name)
        if not args.yes:
            answer = input(f"Restore {path.name}? Current data is backed up first. Type RESTORE: ")
            if answer.strip() != "RESTORE":
                print("Aborted.")
                return 1
        print(json.dumps(backup.restore_backup(settings, path, engine), indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobpilot", description="Local-first job search manager")
    p.add_argument("--debug", action="store_true", help="Show full tracebacks on errors")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create data directories and migrate the database").set_defaults(func=cmd_init)

    s = sub.add_parser("serve", help="Run the local dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("db", help="Apply migrations and show schema revision")
    s.add_argument("action", choices=["upgrade", "current"])
    s.set_defaults(func=cmd_db)

    s = sub.add_parser("backup", help="Create, list or restore backups")
    s.add_argument("action", choices=["create", "list", "restore"])
    s.add_argument("--label")
    s.add_argument("--name", help="Backup file name (in the backup dir) or path, for restore")
    s.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    s.set_defaults(func=cmd_backup)

    from jobpilot import cli_ext

    cli_ext.register(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"jobpilot: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError, LookupError, OSError) as exc:  # expected failure modes
        if args.debug:
            raise
        print(f"jobpilot: error: {exc}\n  (re-run with --debug for details)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Run through the canonical module so ``python -m jobpilot.cli`` and the ``jobpilot``
    # console script share one module instance (and one CliError class).
    from jobpilot.cli import main as _main

    sys.exit(_main())
