"""CLI for tailored resumes: ``jobpilot resume template|preview|generate|regenerate|validate|list|show|path``.

Resumes are made only when you ask, for any job you choose. Exit codes: 0 done · 1 bad input (unknown job, no
template file) · 3 generation or validation failed (details printed; the approved resume is used instead) · 4 no
local LaTeX compiler."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _out(data) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _err(msg: str, code: int) -> int:
    print(msg, file=sys.stderr)
    return code


def _result_code(tr) -> int:
    if tr.status == "valid":
        return 0
    return 4 if tr.compile_status == "unavailable" else 3


def cmd_resume(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.models import Job
    from jobpilot.models.tailored import TailoredResume
    from jobpilot.services import tailored
    from sqlalchemy import select

    settings, engine, factory = _ctx()
    with factory() as s:
        if args.action == "template":
            if not args.file or not Path(args.file).is_file():
                return _err("template needs --file PATH (the LaTeX source of your approved Master resume)", 1)
            try:
                info = tailored.register_template(s, settings, Path(args.file))
            except (tailored.TailorError, UnicodeDecodeError) as exc:
                return _err(f"Not registered: {exc}", 3)
            s.commit()
            _out({"registered": info})
            return 0
        if args.action == "list":
            rows = s.scalars(select(TailoredResume).order_by(TailoredResume.id.desc()).limit(args.limit))
            _out([tailored.as_dict(r) for r in rows])
            return 0
        job = s.get(Job, args.job) if args.job else None
        if job is None:
            return _err(f"{args.action} needs --job ID (an existing job)", 1)
        if args.action == "preview":
            try:
                p = tailored.preview(s, settings, job)
            except tailored.TailorError as exc:
                return _err(f"Can't tailor: {exc}", 3)
            s.rollback()
            _out(p)
            return 0
        if args.action in ("generate", "regenerate"):
            if args.action == "regenerate" or tailored.latest(s, job.id) is None:
                from jobpilot.services import backup

                backup.create_backup(settings, engine, label=f"before-tailored-{job.id}")
            tr = tailored.generate(s, settings, job, force=args.action == "regenerate")
            s.commit()
            _out(tailored.as_dict(tr) | {"decisions": tr.decisions, "validation_checks": (tr.validation or {}).get("checks")})
            return _result_code(tr)
        tr = tailored.latest(s, job.id)
        if tr is None:
            return _err(f"No tailored resume for job #{job.id} yet: jobpilot resume generate --job {job.id}", 1)
        if args.action == "validate":
            tailored.revalidate(s, settings, tr)
            s.commit()
            _out(tailored.as_dict(tr) | {"validation_checks": (tr.validation or {}).get("checks")})
            return 0 if tr.validation_status == "passed" else 3
        if args.action == "show":
            _out(tailored.as_dict(tr) | {"decisions": tr.decisions, "validation": tr.validation})
            return 0
        if args.action == "path":
            if tr.status != "valid" or not tailored.file_ok(settings, tr):
                return _err(f"No valid tailored resume for job #{job.id} ({tailored.summary(tr)})", 3)
            print(settings.files_dir / tr.pdf_path)
            return 0
    return 1


def register(sub) -> None:
    s = sub.add_parser("resume", help="Tailored resumes per job, from your approved Master resume's LaTeX source "
                                      "(re-ordered verified content only; compiled locally; never submits)")
    s.add_argument("action", choices=["template", "preview", "generate", "regenerate", "validate", "list", "show", "path"],
                   help="template: register the LaTeX source of the approved Master resume (--file); preview: the "
                        "tailoring decisions, nothing saved; generate: make (or reuse) the job's PDF and validate it; "
                        "regenerate: make it again even if unchanged; validate: check the stored PDF again; list; "
                        "show: metadata, decisions and checks; path: the validated PDF's path")
    s.add_argument("--job", type=int, help="job id")
    s.add_argument("--file", help="template: path to the .tex source")
    s.add_argument("--limit", type=int, default=50, help="list: how many")
    s.set_defaults(func=cmd_resume)
