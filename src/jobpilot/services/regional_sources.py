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

Added 2026-10-31 (each board checked live for India engineering postings; robots.txt is checked on every request):
Greenhouse — Pure Storage, InMobi, HackerRank, Groww, Toast, Druva, Twilio; Lever — Meesho, Paytm, CRED, Zeta,
FamPay; Workday — Citi, Salesforce, NVIDIA, Intel, Wells Fargo, Target, Motorola Solutions, Workday, Thomson Reuters,
PayPal, CrowdStrike; and Y Combinator startups (sources/yc.py). Left out: SmartRecruiters and Recruitee (their APIs'
robots.txt disallow all agents), Workable (its public job endpoints return no jobs), The Muse (refuses automated
requests), Arbeitnow (Germany only), and Naukri / LinkedIn / Indeed / foundit / Wellfound (their terms forbid
automated collection).

Added 2026-11-01 for fintech and bank / exchange tech centres in India, where capital-markets experience counts
(each board checked live for India engineering postings): Greenhouse — Stripe, Coinbase; Workday — Mastercard,
Morgan Stanley, Deutsche Bank, Nasdaq, State Street, Northern Trust, LSEG, Broadridge, Barclays. Indian fintechs
not covered here (PhonePe, Razorpay, Upstox, Angel One, Dhan, smallcase, Cashfree, Pine Labs) have no Greenhouse
or Lever board; add their career pages by hand. BlackRock and FIS were left out (no India engineering roles).
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
           "vice president",
           # Not SDE / backend (services/roles.py hides any that still get through).
           "frontend", "front end", "android", "ios", "mobile", "react native", "flutter", "qa", "test", "sdet",
           "support", "sales", "salesforce", "data scientist", "analyst", "devops", "site reliability", "sre",
           "firmware", "embedded", "talent pool", "contributor", "coding tasks", "tl", "stl"]
INDIA_CITIES = ["india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "noida", "gurugram",
                "gurgaon", "delhi"]


def _board(name: str, kind: str, key: str, token: str) -> dict[str, Any]:
    return {"name": f"{name} ({kind.capitalize()})", "kind": kind, "company_name": name,
            "config": {key: token, "title_keywords": TITLES + ["engineer"], "exclude_keywords": EXCLUDE,
                       "location_keywords": INDIA_CITIES, "regions": ALL_REGIONS}}


def _workday(name: str, site_url: str) -> dict[str, Any]:
    return {"name": f"{name} (Workday)", "kind": "workday", "company_name": name,
            "config": {"site_url": site_url, "queries": ["software engineer india", "backend developer india"],
                       "title_keywords": TITLES + ["engineer", "computer scientist"], "exclude_keywords": EXCLUDE,
                       "location_keywords": INDIA_CITIES, "regions": ALL_REGIONS, "max_results": 40}}
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
    {"name": "Remote OK (remote)", "kind": "remoteok", "company_name": None,
     "config": {"title_keywords": TITLES, "exclude_keywords": EXCLUDE, "regions": ALL_REGIONS, "max_results": 60},
     "terms_note": "Public API. Remote OK asks to be named and linked as the source; apply links go to its job page."},
    {"name": "Working Nomads (remote)", "kind": "workingnomads", "company_name": None,
     "config": {"categories": ["Development"], "title_keywords": TITLES, "exclude_keywords": EXCLUDE,
                "regions": ALL_REGIONS, "max_results": 60},
     "terms_note": "Public API (robots.txt allows it)."},
    {"name": "We Work Remotely (remote)", "kind": "weworkremotely", "company_name": None,
     "config": {"feeds": ["remote-programming-jobs", "remote-back-end-programming-jobs",
                          "remote-full-stack-programming-jobs"], "title_keywords": TITLES + ["engineer"],
                "exclude_keywords": EXCLUDE, "regions": ALL_REGIONS, "max_results": 60},
     "terms_note": "Public RSS feeds (robots.txt allows them)."},
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
    {"name": "Y Combinator startups (India + remote)", "kind": "yc_jobs", "company_name": None,
     "config": {"locations": ["india", "remote"], "title_keywords": TITLES + ["engineer"], "exclude_keywords": EXCLUDE,
                "regions": ALL_REGIONS, "max_age_days": 180, "max_results": 30},
     "terms_note": "ycombinator.com/jobs (robots.txt allows it); applying needs a YC account."},
    *[_board(n, "greenhouse", "board_token", t) for n, t in (
        ("Pure Storage", "purestorage"), ("InMobi", "inmobi"), ("HackerRank", "hackerrank"), ("Groww", "groww"),
        ("Toast", "toast"), ("Druva", "druva"), ("Twilio", "twilio"), ("Stripe", "stripe"),
        ("Coinbase", "coinbase"))],
    *[_board(n, "lever", "site", t) for n, t in (
        ("Meesho", "meesho"), ("Paytm", "paytm"), ("CRED", "cred"), ("Zeta", "zeta"), ("FamPay", "fampay"))],
    *[_workday(n, u) for n, u in (
        ("Citi", "https://citi.wd5.myworkdayjobs.com/2"),
        ("Salesforce", "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"),
        ("NVIDIA", "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"),
        ("Intel", "https://intel.wd1.myworkdayjobs.com/External"),
        ("Wells Fargo", "https://wf.wd1.myworkdayjobs.com/WellsFargoJobs"),
        ("Target", "https://target.wd5.myworkdayjobs.com/targetcareers"),
        ("Motorola Solutions", "https://motorolasolutions.wd5.myworkdayjobs.com/Careers"),
        ("Workday", "https://workday.wd5.myworkdayjobs.com/Workday"),
        ("Thomson Reuters", "https://thomsonreuters.wd5.myworkdayjobs.com/External_Career_Site"),
        ("PayPal", "https://paypal.wd1.myworkdayjobs.com/jobs"),
        ("CrowdStrike", "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers"),
        # Fintech and bank / exchange tech centres (added 2026-11-01).
        ("Mastercard", "https://mastercard.wd1.myworkdayjobs.com/CorporateCareers"),
        ("Morgan Stanley", "https://ms.wd5.myworkdayjobs.com/External"),
        ("Deutsche Bank", "https://db.wd3.myworkdayjobs.com/DBWebsite"),
        ("Nasdaq", "https://nasdaq.wd1.myworkdayjobs.com/Global_External_Site"),
        ("State Street", "https://statestreet.wd1.myworkdayjobs.com/Global"),
        ("Northern Trust", "https://ntrs.wd1.myworkdayjobs.com/northerntrust"),
        ("LSEG", "https://lseg.wd3.myworkdayjobs.com/Careers"),
        ("Broadridge", "https://broadridge.wd5.myworkdayjobs.com/Careers"),
        ("Barclays", "https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays"))],
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
