"""Y Combinator startups' jobs (Work at a Startup), from ycombinator.com/jobs.

Checked 2026-10-31: robots.txt allows ``/jobs`` and ``/companies/<name>/jobs/<id>`` (it disallows only query strings on
``/companies``, never used here). Each listing page (``/jobs/role/software-engineer/<india|remote>``) embeds its
postings as JSON for its own rendering (title, company, location, minimum experience, age); each posting's page
carries a schema.org JobPosting (the description). Only postings that pass your title, location and age filters are
opened, at most ``max_results`` per run. If YC changes the page structure the run fails instead of guessing.

Locations use country codes ("IN / Remote (IN)"); they are written out ("India / Remote (India)") so the region
filter understands them. A listing is one page, a subset of YC's jobs, so a job missing from one run is never
expired because of it. Applying needs a YC account: the apply link is the posting's page.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone

from jobpilot.services.text_utils import clean_ws
from jobpilot.sources.base import FetchResult, JobData
from jobpilot.sources.careers import _base_config, _limit, _regions, _result, location_wanted, title_wanted
from jobpilot.sources.http import FetchError, PoliteHttp
from jobpilot.sources.pages import extract_from_html

BASE = "https://www.ycombinator.com"
LOCATIONS = {"india", "remote", "san-francisco", "new-york", "los-angeles", "seattle", "austin", "chicago"}
CODES = {"IN": "India", "AE": "United Arab Emirates", "US": "United States", "GB": "United Kingdom", "CA": "Canada",
         "SG": "Singapore", "DE": "Germany", "FR": "France", "NL": "Netherlands", "IE": "Ireland", "ES": "Spain",
         "BR": "Brazil", "MX": "Mexico", "AU": "Australia", "IL": "Israel", "CH": "Switzerland", "SE": "Sweden",
         "HK": "Hong Kong", "JP": "Japan", "TR": "Turkey", "PK": "Pakistan", "ID": "Indonesia", "PH": "Philippines"}
_DATA_PAGE = re.compile(r'data-page="([^"]*)"')
_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])")


def location_text(value: str | None) -> str:
    """"Bengaluru, KA, IN / Remote (IN)" → "Bengaluru, KA, India / Remote (India)" (state codes are left alone,
    only two-letter codes at the end of a place or alone are countries)."""
    parts = []
    for place in re.split(r"\s*/\s*", clean_ws(value or "")):
        inner = re.match(r"^(Remote)\s*\((.*)\)$", place)
        if inner:
            subs = [_country(s) for s in re.split(r"\s*;\s*", inner.group(2))]
            parts.append(f"Remote ({'; '.join(subs)})")
        else:
            parts.append(_country(place))
    return " / ".join(p for p in parts if p)


def _country(place: str) -> str:
    bits = [b.strip() for b in place.split(",")]
    if bits and _CODE.fullmatch(bits[-1]) and bits[-1] in CODES:
        bits[-1] = CODES[bits[-1]]
    return ", ".join(bits)


def page_data(text: str) -> dict:
    m = _DATA_PAGE.search(text)
    if not m:
        raise FetchError("YC jobs page: no embedded data (page structure changed)")
    try:
        return json.loads(html.unescape(m.group(1))).get("props") or {}
    except (json.JSONDecodeError, AttributeError) as exc:
        raise FetchError("YC jobs page: unreadable embedded data") from exc


class YCJobsAdapter:
    kind = "yc_jobs"
    label = "Y Combinator startups (Work at a Startup job listings)"
    config_help = ('{"locations": ["india", "remote"], "title_keywords": ["software engineer", "backend", '
                   '"full stack"], "exclude_keywords": ["senior", "staff", "lead"], "regions": ["india", "remote", '
                   '"dubai"], "max_age_days": 180, "max_results": 30}')

    def validate_config(self, config: dict) -> dict:
        locs = config.get("locations") or ["india", "remote"]
        if isinstance(locs, str):
            locs = re.split(r"[,\n]", locs)
        locs = list(dict.fromkeys(str(x).strip().lower() for x in locs if str(x).strip()))
        if not locs or any(x not in LOCATIONS for x in locs):
            raise ValueError(f"locations: use values among {sorted(LOCATIONS)}")
        cfg = {**config, "queries": config.get("queries") or ["software engineer"]}
        return {**_base_config(cfg, 30, 60), "locations": locs, "regions": _regions(config),
                "max_age_days": _limit(config, "max_age_days", 180, 3650)}

    def listing(self, http: PoliteHttp, location: str) -> list[dict]:
        r = http.get(f"{BASE}/jobs/role/software-engineer/{location}", accept="text/html")
        posts = page_data(r.text).get("jobPostings")
        if not isinstance(posts, list):
            raise FetchError("YC jobs page: no job list (page structure changed)")
        return [p for p in posts if isinstance(p, dict) and p.get("title") and str(p.get("url") or "").startswith("/companies/")]

    def _job(self, http: PoliteHttp, p: dict, company_name: str | None) -> JobData | None:
        url = BASE + p["url"]
        r = http.get(url, accept="text/html")
        found, _ = extract_from_html(r.text, url, company_name or p.get("companyName"))
        job = found[0] if found and (found[0].raw or {}).get("jsonld") else None
        if job is None:
            raise FetchError(f"YC posting {url}: no JobPosting data")
        extra = [f"Experience: {p['minExperience']}" if p.get("minExperience") else "",
                 f"Visa: {p['visa']}" if p.get("visa") else "", f"Salary: {p['salaryRange']}" if p.get("salaryRange") else ""]
        extra = "\n".join(x for x in extra if x)
        if extra:
            job.description_text = f"{job.description_text}\n\n{extra}".strip()
        loc = location_text(p.get("location"))
        job.title = clean_ws(p["title"])
        job.company_name = company_name or p.get("companyName") or job.company_name
        job.location = loc or job.location
        job.remote_type = "remote" if re.search(r"\bremote\b", loc, re.I) else job.remote_type
        job.source_url = job.apply_url = url
        job.external_id = str(p.get("id") or url)
        job.employment_type = p.get("type") or job.employment_type
        job.raw = {**(job.raw or {}), "board": "yc", "batch": p.get("companyBatchName"), "role_type":
                   p.get("roleSpecificType"), "min_experience": p.get("minExperience"), "age": p.get("createdAt")}
        return job

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        wanted: dict[str, dict] = {}
        seen = 0
        for loc in cfg["locations"]:
            posts = self.listing(http, loc)
            seen += len(posts)
            for p in posts:
                if title_wanted(cfg, p["title"]) and location_wanted(cfg, location_text(p.get("location")),
                                                                        None, p["title"]):
                    wanted.setdefault(str(p.get("id") or p["url"]), p)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=cfg["max_age_days"])
        items, errors, old = [], [], 0
        for p in list(wanted.values())[:cfg["max_results"]]:
            try:
                job = self._job(http, p, company_name)
            except FetchError as exc:
                errors.append(str(exc)[:300])
                continue
            if job.posted_at and job.posted_at < cutoff:
                old += 1
                continue
            items.append(job)
        res = _result(items, seen, cfg, "html", errors=errors)
        if old:
            res.notes.append(f"{old} posting(s) older than {cfg['max_age_days']} days left out")
        return res
