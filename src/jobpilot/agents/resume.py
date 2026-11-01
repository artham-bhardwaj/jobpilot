"""Resume agent: a validated resume tailored to each job the Manager picked.

For each job it makes (or reuses, when nothing changed) the tailored PDF from your approved Master resume: bullets,
projects and skills re-ordered for the job, skills spelled the way the posting spells them (pure synonyms only),
education years from your confirmed records, and the 13 checks (same content as the Master, nothing clipped, page
limit, …). Nothing is invented: a job skill you don't have is listed as missing, never added.

If a tailored resume can't be made or fails a check, the approved resume chosen for the job is used instead
(``fallback_to_master_resume``), or the job waits for you.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from jobpilot.agents.base import Report
from jobpilot.config import Settings
from jobpilot.models import Job

log = logging.getLogger(__name__)


def run(session: Session, settings: Settings, pol: dict[str, Any], jobs: list[Job]) -> tuple[Report, dict[int, Any]]:
    """Returns the report and {job_id: {"kind": "tailored"|"master", "tailored": tr, "version": v}} for each job that
    has a usable resume."""
    from jobpilot.services import resumes as resume_svc
    from jobpilot.services import tailored

    rep = Report("resume")
    ready: dict[int, Any] = {}
    for job in jobs:
        sel = resume_svc.select_for_job(session, job)
        version = None if sel.get("missing") else sel.get("version")
        try:
            tr = tailored.generate(session, settings, job)
        except tailored.TailorError as exc:
            tr, why = None, str(exc)
        except Exception as exc:  # compiler crash and the like: never stop the cycle
            log.exception("resume agent: generate failed")
            tr, why = None, f"{type(exc).__name__}: {str(exc)[:200]}"
        else:
            why = (tr.error or "") if tr.status != "valid" else ""
        session.commit()
        if tr is not None and tr.status == "valid" and tailored.file_ok(settings, tr):
            words = (tr.decisions or {}).get("rewording") or []
            note = f"; reworded {', '.join(w['from'] + '→' + w['to'] for w in words)}" if words else ""
            rep.item(job, "tailored", f"tailored resume #{tr.id}, {tr.page_count or '?'} page(s), all checks passed{note}",
                     tailored_resume_id=tr.id)
            ready[job.id] = {"kind": "tailored", "tailored": tr, "version": version}
        elif pol["fallback_to_master_resume"] and version is not None:
            rep.item(job, "master", f"tailoring didn't pass ({why[:200] or 'no tailored resume'}); using approved "
                                    f"{version.resume.name} v{version.version_no}")
            ready[job.id] = {"kind": "master", "tailored": None, "version": version}
        else:
            rep.item(job, "no_resume", why[:300] or sel.get("reason") or "no approved resume fits this job")
    c = rep.counts
    rep.summary = (f"{c.get('tailored', 0)} tailored resume(s) ready"
                   + (f"; {c['master']} using your approved resume" if c.get("master") else "")
                   + (f"; {c['no_resume']} without a usable resume" if c.get("no_resume") else ""))
    return rep, ready
