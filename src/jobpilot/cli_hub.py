"""CLI for the personal information hub: ``jobpilot hub import|report|link-docs``."""

from __future__ import annotations

import json
from pathlib import Path


def _summary(imp) -> dict:
    counts = dict(imp.counts or {})
    counts.pop("by_category", None)
    report = imp.report or {}
    return {"import_id": imp.id, "status": imp.status, "file": imp.source_filename, "sha256": imp.source_sha256,
            "extraction": imp.extraction_method, "resume_version_id": imp.resume_version_id, "counts": counts,
            "by_category": (imp.counts or {}).get("by_category"), "warnings": report.get("warnings"),
            "verification": report.get("verification"), "backup": imp.backup_path, "error": imp.error,
            "needs_attention": [{"outcome": it["outcome"], "key": it["key"], "resume": it["value"],
                                 "profile": it["existing"]} for it in report.get("items", [])
                                if it["outcome"] in ("conflict", "suggestion")]}


def cmd_hub(args) -> int:
    from sqlalchemy import select

    from jobpilot.cli import CliError, _ctx
    from jobpilot.db import session_scope
    from jobpilot.models import ProfileImport, Project, ResumeVersion
    from jobpilot.services import hub

    settings, _, factory = _ctx()
    if args.action == "import":
        if bool(args.file) == bool(args.version):
            raise CliError("Give exactly one of --file PATH or --version ID", code=2)
        imp_id, failure = None, None
        with session_scope(factory) as s:  # commits the import, or the record of a failed attempt
            try:
                if args.file:
                    path = Path(args.file).expanduser()
                    if not path.is_file():
                        raise CliError(f"No such file: {path}", code=2)
                    imp = hub.import_resume(s, settings, path.name, path.read_bytes(), dry_run=args.dry_run)
                else:
                    version = s.get(ResumeVersion, args.version)
                    if version is None:
                        raise CliError(f"Resume version #{args.version} not found", code=2)
                    imp = hub.import_registered_version(s, settings, version, dry_run=args.dry_run)
                imp_id = imp.id
            except hub.HubImportError as exc:
                failure = str(exc)
        if failure:
            raise CliError(f"Import failed: {failure}")
        with factory() as s:  # read back from the database, after the commit
            print(json.dumps(_summary(s.get(ProfileImport, imp_id)), indent=2, default=str, ensure_ascii=False))
        return 0
    if args.action == "report":
        with factory() as s:
            imp = s.get(ProfileImport, args.id) if args.id else \
                s.scalar(select(ProfileImport).order_by(ProfileImport.id.desc()).limit(1))
            if imp is None:
                raise CliError("No imports yet", code=2)
            print(json.dumps(_summary(imp), indent=2, default=str, ensure_ascii=False))
        return 0
    if args.action == "link-docs":
        if not args.project or not args.path:
            raise CliError("link-docs needs --project NAME and --path DIR", code=2)
        with session_scope(factory) as s:
            project = next((p for p in s.scalars(select(Project)) if p.name.lower() == args.project.lower()), None)
            if project is None:
                raise CliError(f"No project named {args.project!r}", code=2)
            claims = hub.link_project_docs(s, project, args.path)
            print(json.dumps([{"field": c.field, "value": c.value, "location": c.source_location, "notes": c.notes}
                              for c in claims], indent=2, ensure_ascii=False))
        return 0
    raise CliError(f"Unknown action {args.action}", code=2)


def register(sub) -> None:
    s = sub.add_parser("hub", help="Personal information hub: import a resume into your profile, show import reports")
    s.add_argument("action", choices=["import", "report", "link-docs"])
    s.add_argument("--file", help="import: resume file (PDF/DOCX/TXT); stored unchanged and registered if new")
    s.add_argument("--version", type=int, help="import: an existing resume version id (re-import its stored file)")
    s.add_argument("--dry-run", action="store_true", help="import: report what would change, save nothing")
    s.add_argument("--id", type=int, help="report: import id (default: the latest)")
    s.add_argument("--project", help="link-docs: project name")
    s.add_argument("--path", help="link-docs: the project's local directory (README, docs/ and git remote are read)")
    s.set_defaults(func=cmd_hub)
