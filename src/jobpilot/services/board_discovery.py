"""Find the official job boards of companies you've seen in feeds and alert emails, and add them as sources.

Rule-based, for the Search agent. For each company that appears in your active jobs only through an aggregator
(Remote OK, We Work Remotely, Himalayas, a job-alert email, …) and has no board source yet, a few likely board names
are tried on the public board APIs:

- Greenhouse ``boards-api.greenhouse.io/v1/boards/<name>``: accepted only when the board's own name is the company.
- Lever ``api.lever.co/v0/postings/<name>``: Lever doesn't return the company name, so only the exact company name
  (letters and digits only, e.g. "mindtickle") is tried, and only a board with at least one posting is accepted.

Not tried: Ashby (its robots.txt disallows the job API) and Workday (each employer's site is different).
Every request goes through PoliteHttp (robots.txt, rate limits). Each company is checked at most once every 30 days.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Job, JobSource, utcnow
from jobpilot.services import audit, notifications
from jobpilot.services.text_utils import normalize_company
from jobpilot.sources.http import FetchError

log = logging.getLogger(__name__)

STATE_KEY = "agents.board_probes"
RECHECK_DAYS = 30
BOARD_KINDS = ("greenhouse", "lever", "ashby", "workday")
AGGREGATOR_KINDS = ("remoteok", "workingnomads", "weworkremotely", "himalayas", "jobicy", "yc_jobs", "manual", "jsonld",
                    "sitemap")
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{}"
LEVER = "https://api.lever.co/v0/postings/{}"


_LEGAL = {"inc", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation", "co", "gmbh", "plc", "llp", "sa",
          "bv", "ag", "the"}


def slugs(company: str) -> list[str]:
    """Likely board names: "Acme Labs Pvt. Ltd." → ["acmelabs", "acme-labs", "acme"] (legal suffixes dropped; the
    shorter company name JobPilot uses for matching is tried too)."""
    from jobpilot.services.text_utils import normalize

    full = [w for w in re.split(r"[^a-z0-9]+", normalize(company)) if w and w not in _LEGAL]
    short = [w for w in re.split(r"[^a-z0-9]+", normalize_company(company)) if w]
    out = []
    for words in (full, short):
        if words:
            out += ["".join(words), "-".join(words)]
    if len(full) > 1 and len(full[0]) >= 4:
        out.append(full[0])
    return [x for x in dict.fromkeys(out) if 2 <= len(x) <= 60]


def _covered(session: Session) -> set[str]:
    """Companies that already have a board source (normalized)."""
    out = set()
    for src in session.scalars(select(JobSource).where(JobSource.kind.in_(BOARD_KINDS))):
        if src.company_name:
            out.add(normalize_company(src.company_name))
    return out


def candidates(session: Session, limit: int) -> list[str]:
    """Companies seen only through aggregators, best-scoring first, not checked in the last 30 days."""
    covered = _covered(session)
    probed = (notifications.get_state(session, STATE_KEY) or {}).get("companies", {})
    cutoff = (utcnow() - timedelta(days=RECHECK_DAYS)).isoformat()
    agg_ids = set(session.scalars(select(JobSource.id).where(JobSource.kind.in_(AGGREGATOR_KINDS))))
    best: dict[str, tuple[float, str]] = {}
    for job in session.scalars(select(Job).where(Job.status == "active", Job.company_name.is_not(None))):
        if not any(s.source_id in agg_ids for s in job.sightings):
            continue
        key = normalize_company(job.company_name)
        if not key or key in covered or (probed.get(key, {}).get("at") or "") > cutoff:
            continue
        score = job.relevance_score or 0
        if key not in best or score > best[key][0]:
            best[key] = (score, job.company_name)
    return [name for _, name in sorted(best.values(), key=lambda x: -x[0])][:limit]


def _board_name(name: str | None) -> str:
    """A board's name without generic words: "Rubrik Job Board" → "rubrik"."""
    n = normalize_company(name)
    return re.sub(r"\s*\b(?:job board|jobs board|careers?|jobs|hiring)\b\s*$", "", n).strip()


def probe(http, company: str) -> dict[str, Any] | None:
    """The company's Greenhouse or Lever board, or None. Raises nothing for a missing board."""
    key = normalize_company(company)
    for slug in slugs(company):
        try:
            data = http.get_json(GREENHOUSE.format(slug))
        except FetchError:
            data = None
        if isinstance(data, dict) and _board_name(data.get("name")) == key:
            return {"kind": "greenhouse", "token": slug, "config_key": "board_token"}
    names = slugs(company)
    exact = names[0] if names else ""
    if exact:
        try:
            data = http.get_json(LEVER.format(exact), params={"mode": "json", "limit": 1})
        except FetchError:
            data = None
        if isinstance(data, list) and data:
            return {"kind": "lever", "token": exact, "config_key": "site"}
    return None


def run(session: Session, http, limit: int = 10) -> dict[str, Any]:
    """Check up to ``limit`` companies and add the boards found as enabled sources (titles and regions filtered)."""
    from jobpilot.services import jobs as job_svc
    from jobpilot.services.regional_sources import ALL_REGIONS, EXCLUDE, TITLES

    import copy

    # a copy: the stored JSON must be replaced, not changed in place, for the change to be saved
    state = copy.deepcopy(notifications.get_state(session, STATE_KEY) or {"companies": {}})
    added, checked, errors = [], [], []
    for company in candidates(session, limit):
        key = normalize_company(company)
        checked.append(company)
        try:
            found = probe(http, company)
        except Exception as exc:  # network trouble: try again next time, never fail the whole search
            errors.append(f"{company}: {type(exc).__name__}")
            continue
        state["companies"][key] = {"at": utcnow().isoformat(timespec="seconds"),
                                   "found": f"{found['kind']}:{found['token']}" if found else None}
        if not found:
            continue
        label = "Greenhouse" if found["kind"] == "greenhouse" else "Lever"
        name = f"{company} ({label})"
        if session.scalar(select(JobSource.id).where(JobSource.name == name)) is not None:
            continue
        try:
            job_svc.save_source(session, job_svc.SourceIn(
                name=name, kind=found["kind"], company_name=company, enabled=True,
                config={found["config_key"]: found["token"], "title_keywords": TITLES, "exclude_keywords": EXCLUDE,
                        "regions": ALL_REGIONS},
                terms_note="Added by the Search agent: the company's public job board API"))
        except ValueError as exc:
            errors.append(f"{company}: {exc}")
            continue
        audit.record(session, "agents.board_added", "job_source", None, actor="system", company=company,
                     kind=found["kind"], token=found["token"])
        added.append(name)
    notifications.set_state(session, STATE_KEY, state)
    return {"checked": checked, "added": added, "errors": errors}
