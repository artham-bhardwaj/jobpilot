"""Search agent: find every relevant job it is allowed to find.

Each cycle:
1. Makes sure the public feeds and regional boards are sources (Remote OK, Working Nomads, We Work Remotely,
   Himalayas, Jobicy and the Dubai / India boards; ``add_feed_sources``). Your own sources are never changed.
2. Runs every enabled source when the last run is older than ``search_every_hours`` (the sources score new jobs).
3. Reads job-alert emails from LinkedIn, Naukri, Indeed, Glassdoor, foundit and Instahyre in your Gmail
   (``job_alert_emails``; needs JOBPILOT_GMAIL_ALLOW_READ_BODIES=true). Those sites are never visited.
4. Looks up the official Greenhouse / Lever boards of companies seen only in feeds and alerts, and adds them
   (``discover_boards``), so the next run collects the employer's own postings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from jobpilot.agents.base import Report
from jobpilot.config import Settings
from jobpilot.models import utcnow
from jobpilot.services import notifications

log = logging.getLogger(__name__)

LAST_COLLECT = "agents.search.last_collect"


def _collect_due(session: Session, every_hours: float) -> bool:
    last = (notifications.get_state(session, LAST_COLLECT) or {}).get("at")
    if not last:
        return True
    try:
        return utcnow() - datetime.fromisoformat(last) >= timedelta(hours=every_hours)
    except ValueError:
        return True


def run(session: Session, settings: Settings, pol: dict[str, Any], *, http=None, gmail_client=None,
        force_collect: bool = False) -> Report:
    from jobpilot.services import board_discovery, regional_sources
    from jobpilot.services import jobs as job_svc
    from jobpilot.sources.http import PoliteHttp

    rep = Report("search")
    http = http or PoliteHttp(settings)
    if pol["add_feed_sources"]:
        todo = regional_sources.plan(session)
        added = regional_sources.apply(session) if (todo["add"] or todo["extend"]) else todo
        if added.get("add"):
            rep.add("sources_added", len(added["add"]))
            rep.items.append({"outcome": "sources_added", "detail": ", ".join(added["add"])[:500]})
        session.commit()

    if force_collect or _collect_due(session, float(pol["search_every_hours"])):
        try:
            res = job_svc.collect_all(session, settings, http, trigger="agents")
            rep.add("sources_run", len(res["sources"]))
            rep.add("new_jobs", int(res.get("new") or 0))
            rep.add("sources_failed", int(res.get("failed") or 0))
            failed = [n for n, v in res["sources"].items() if v.get("status") in ("failed", "unavailable")]
            if failed:
                rep.errors.append("sources failed: " + ", ".join(failed[:10]))
            notifications.set_state(session, LAST_COLLECT, {"at": utcnow().isoformat(timespec="seconds"),
                                                            "new": int(res.get("new") or 0)})
        except Exception as exc:  # one bad run must not stop resumes and applications for jobs already found
            log.exception("search: collection failed")
            rep.errors.append(f"collection failed: {type(exc).__name__}: {str(exc)[:200]}")
        session.commit()
    else:
        rep.add("collection_skipped")

    if pol["job_alert_emails"]:
        alerts = _alerts(session, settings, gmail_client, rep)
        if alerts:
            rep.add("alert_emails", alerts["emails"])
            rep.add("new_jobs", alerts["new"])
            rep.add("alert_jobs_new", alerts["new"])
        session.commit()

    if pol["discover_boards"] and int(pol["max_board_checks"]) > 0:
        try:
            found = board_discovery.run(session, http, int(pol["max_board_checks"]))
            rep.add("companies_checked", len(found["checked"]))
            rep.add("boards_added", len(found["added"]))
            if found["added"]:
                rep.items.append({"outcome": "boards_added", "detail": ", ".join(found["added"])[:500]})
            rep.errors += found["errors"][:5]
        except Exception as exc:
            log.exception("search: board discovery failed")
            rep.errors.append(f"board discovery failed: {type(exc).__name__}")
        session.commit()

    c = rep.counts
    rep.summary = (f"{c.get('new_jobs', 0)} new job(s)"
                   + (f" from {c['sources_run']} source(s)" if c.get("sources_run") else " (sources ran recently)")
                   + (f"; {c['alert_emails']} alert email(s) read" if c.get("alert_emails") else "")
                   + (f"; {c['boards_added']} company board(s) added" if c.get("boards_added") else ""))
    return rep


def _alerts(session: Session, settings: Settings, client, rep: Report) -> dict | None:
    from jobpilot.integrations import gmail
    from jobpilot.services import job_alerts

    if client is None:
        if not gmail.is_ready(settings):
            rep.add("alerts_unavailable")
            return None
        if not settings.gmail_allow_read_bodies:
            rep.items.append({"outcome": "alerts_off", "detail": "Reading job-alert emails needs "
                              "JOBPILOT_GMAIL_ALLOW_READ_BODIES=true and `jobpilot gmail auth` again"})
            return None
        client = gmail.GmailClient(settings)
    try:
        return job_alerts.import_recent(session, settings, client)
    except Exception as exc:
        log.exception("search: job alerts failed")
        rep.errors.append(f"job-alert emails: {type(exc).__name__}: {str(exc)[:160]}")
        return None
