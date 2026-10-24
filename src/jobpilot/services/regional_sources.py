"""Recommended sources for India, remote and Dubai / UAE jobs, added in one step (Sources → "Add regional sources",
or ``jobpilot sources regional``).

- New sources are added only when no source of that name exists (yours are never overwritten).
- Existing whole-board sources that filter by location (Greenhouse, Lever, Ashby, Workday, Atlassian) also get
  ``regions: ["india", "remote", "dubai"]``, so their remote roles open to India / the UAE and their Dubai roles are
  kept as well as the cities you listed. Nothing else in them changes.

Boards checked 2026-10-23: Bybit, Binance, OKX and Careem (Greenhouse) list Dubai / UAE roles (mostly senior ones, so
few pass the experience filter); Ziina (Ashby) was left out because Ashby's robots.txt disallows its job API;
Himalayas and Jobicy list remote roles (see sources/remote.py). Amazon and Microsoft have few UAE engineering roles
but are included for completeness; Google listed none in the UAE.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import JobSource
from jobpilot.services import audit
from jobpilot.services import jobs as job_svc

ALL_REGIONS = ["india", "remote", "dubai"]
TITLES = ["software engineer", "software development engineer", "software developer", "backend", "back end",
          "developer", "sde", "full stack", "java", "python"]
EXCLUDE = ["architect", "director", "intern", "lead", "manager", "mgr", "principal", "senior", "sr", "staff", "head",
           "vice president"]
REGION_FILTERED_KINDS = ("greenhouse", "lever", "ashby", "workday", "atlassian_careers")

PRESETS: list[dict[str, Any]] = [
    {"name": "Himalayas (remote, open to India/UAE)", "kind": "himalayas", "company_name": None,
     "config": {"queries": ["software engineer", "backend engineer", "java developer", "python developer"],
                "countries": ["India", "United Arab Emirates"], "title_keywords": TITLES, "exclude_keywords": EXCLUDE,
                "regions": ALL_REGIONS, "max_results": 80},
     "terms_note": "Public search API; first page per query only (robots.txt disallows paging)."},
    {"name": "Jobicy (remote)", "kind": "jobicy", "company_name": None,
     "config": {"geos": ["anywhere", "apac", "emea", "united-arab-emirates"], "industry": "engineering",
                "title_keywords": TITLES, "exclude_keywords": EXCLUDE, "regions": ALL_REGIONS, "max_results": 60},
     "terms_note": "Public API. Jobicy asks to be credited and that apply links go to its job page (they do)."},
    {"name": "Bybit (Greenhouse)", "kind": "greenhouse", "company_name": "Bybit",
     "config": {"board_token": "bybit", "title_keywords": TITLES, "exclude_keywords": EXCLUDE, "regions": ALL_REGIONS}},
    {"name": "Binance (Greenhouse)", "kind": "greenhouse", "company_name": "Binance",
     "config": {"board_token": "binance", "title_keywords": TITLES, "exclude_keywords": EXCLUDE,
                "regions": ALL_REGIONS}},
    {"name": "OKX (Greenhouse)", "kind": "greenhouse", "company_name": "OKX",
     "config": {"board_token": "okx", "title_keywords": TITLES, "exclude_keywords": EXCLUDE, "regions": ALL_REGIONS}},
    {"name": "Careem (Greenhouse)", "kind": "greenhouse", "company_name": "Careem",
     "config": {"board_token": "careem", "title_keywords": TITLES + ["engineer"], "exclude_keywords": EXCLUDE,
                "regions": ALL_REGIONS}},
    {"name": "Amazon UAE (amazon.jobs)", "kind": "amazon_jobs", "company_name": "Amazon",
     "config": {"queries": ["software development engineer", "software engineer"], "country": "ARE",
                "title_keywords": ["software development engineer", "software dev engineer", "software engineer",
                                   "sde"], "exclude_keywords": EXCLUDE + ["ii", "iii"], "max_results": 50}},
    {"name": "Microsoft UAE (careers)", "kind": "microsoft_careers", "company_name": "Microsoft",
     "config": {"queries": ["software engineer"], "location": "United Arab Emirates",
                "title_keywords": ["software engineer"], "exclude_keywords": EXCLUDE, "max_results": 20,
                "seniority": ["Entry", "Mid-Level"]}},
]


def plan(session: Session) -> dict[str, list[str]]:
    """What ``apply`` would do, without changing anything."""
    names = set(session.scalars(select(JobSource.name)))
    add = [p["name"] for p in PRESETS if p["name"] not in names]
    extend = [s.name for s in session.scalars(select(JobSource).where(JobSource.kind.in_(REGION_FILTERED_KINDS)))
              if (s.config or {}).get("location_keywords") and set(ALL_REGIONS) - set((s.config or {}).get("regions") or [])]
    return {"add": add, "extend": extend}


def apply(session: Session) -> dict[str, list[str]]:
    """Add the missing presets and give location-filtered board sources the three regions."""
    todo = plan(session)
    for p in PRESETS:
        if p["name"] in todo["add"]:
            job_svc.save_source(session, job_svc.SourceIn(name=p["name"], kind=p["kind"],
                                                          company_name=p["company_name"], config=p["config"],
                                                          enabled=True, terms_note=p.get("terms_note")))
    for src in session.scalars(select(JobSource).where(JobSource.name.in_(todo["extend"]))):
        cfg = dict(src.config or {})
        cfg["regions"] = list(dict.fromkeys([*(cfg.get("regions") or []), *ALL_REGIONS]))
        job_svc.save_source(session, job_svc.SourceIn(name=src.name, kind=src.kind, company_name=src.company_name,
                                                      config=cfg, enabled=src.enabled, terms_note=src.terms_note),
                            src.id)
    audit.record(session, "sources.regional_added", "job_source", None, added=todo["add"], extended=todo["extend"])
    return todo
