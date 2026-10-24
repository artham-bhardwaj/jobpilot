"""Remote-job boards with public JSON APIs, for remote roles open to candidates in India or the UAE.

Checked 2026-10-23 (robots.txt and the providers' own API notes):

- Himalayas: ``https://himalayas.app/jobs/api/search?q=…&country=…`` (public, no key). robots.txt disallows
  ``page=`` on ``/jobs`` URLs, so only the first page (20 results) of each query is read, never further pages.
  ``locationRestrictions`` lists the countries a remote job is open to (empty = worldwide).
- Jobicy: ``https://jobicy.com/api/v2/remote-jobs?count=…&geo=…&industry=…`` (public, no key). Jobicy asks that it
  is credited with a link and that "apply" goes to the job URL in the feed: each job keeps that URL as its source
  and apply link, and the source is named Jobicy.
- Not usable: Remotive (robots.txt disallows ``/api/``).

Both return a subset of the board, so a job missing from one run is never expired because of it. Postings outside
your regions (e.g. "Remote — United States") are dropped before anything is stored.
"""

from __future__ import annotations

import re
from typing import Any

from jobpilot.services.text_utils import clean_ws
from jobpilot.sources.base import FetchResult, JobData, parse_dt
from jobpilot.sources.careers import _base_config, _job, _limit, _regions, _result, location_wanted, title_wanted


def _list(config: dict, key: str, default: list[str], allowed: set[str] | None = None, top: int = 6) -> list[str]:
    v = config.get(key)
    if v in (None, "", []):
        v = default
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    out = list(dict.fromkeys(clean_ws(str(x))[:60] for x in v if clean_ws(str(x))))
    if len(out) > top:
        raise ValueError(f"{key}: at most {top} values")
    if allowed is not None and any(x not in allowed for x in out):
        raise ValueError(f"{key}: use values among {sorted(allowed)}")
    return out


class HimalayasAdapter:
    kind = "himalayas"
    label = "Himalayas remote jobs (search API; remote roles open to your countries)"
    config_help = ('{"queries": ["software engineer", "backend engineer"], "countries": ["India", '
                   '"United Arab Emirates"], "title_keywords": ["software engineer", "backend", "developer"], '
                   '"exclude_keywords": ["senior", "staff", "lead", "manager"], "regions": ["remote", "india", "dubai"], '
                   '"max_results": 60}')
    URL = "https://himalayas.app/jobs/api/search"

    def validate_config(self, config: dict) -> dict:
        return {**_base_config(config, 60, 200),
                "countries": _list(config, "countries", ["India", "United Arab Emirates"]),
                "regions": _regions(config) or ["remote", "india", "dubai"]}

    @staticmethod
    def _location(j: dict) -> str:
        where = [clean_ws(str(x)) for x in j.get("locationRestrictions") or [] if clean_ws(str(x))]
        return "Remote — " + (", ".join(where[:6]) if where else "Worldwide")

    def _item(self, j: dict, company_name: str | None) -> JobData:
        url = j.get("guid") or j.get("applicationLink")
        lo, hi = j.get("minSalary"), j.get("maxSalary")
        return _job(j.get("description") or j.get("excerpt") or "", title=clean_ws(j.get("title") or ""),
                    company_name=company_name or j.get("companyName"), location=self._location(j), remote_type="remote",
                    posted_at=parse_dt(j.get("pubDate")), valid_through=parse_dt(j.get("expiryDate")),
                    source_url=url, apply_url=j.get("applicationLink") or url, external_id=url,
                    employment_type=j.get("employmentType"),
                    salary_min=int(lo) if isinstance(lo, (int, float)) else None,
                    salary_max=int(hi) if isinstance(hi, (int, float)) else None,
                    salary_currency=j.get("currency") if lo or hi else None,
                    salary_period=("year" if j.get("salaryPeriod") == "annual" else j.get("salaryPeriod")) if lo or hi else None,
                    raw={"board": "himalayas", "company_slug": j.get("companySlug"), "seniority": j.get("seniority"),
                         "location_restrictions": j.get("locationRestrictions"), "categories": (j.get("categories") or [])[:8]})

    def fetch(self, http, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        items: dict[str, JobData] = {}
        seen = 0
        for q in cfg["queries"]:
            for country in cfg["countries"] or [None]:
                params: dict[str, Any] = {"q": q, "sort": "recent"}
                if country:
                    params["country"] = country
                data = http.get_json(self.URL, params=params)
                jobs = data.get("jobs") if isinstance(data, dict) else None
                if not isinstance(jobs, list):
                    return FetchResult(errors=["Unexpected Himalayas response shape"], method="api")
                seen += len(jobs)
                for j in jobs:
                    if not (isinstance(j, dict) and j.get("title") and (j.get("guid") or j.get("applicationLink"))):
                        continue
                    if not title_wanted(cfg, j["title"]):
                        continue
                    job = self._item(j, company_name)
                    if location_wanted(cfg, job.location, "remote", job.title):
                        items.setdefault(job.external_id, job)
        res = _result(list(items.values()), seen, cfg, "api")
        res.notes.append("first results page per query and country only (robots.txt disallows paging)")
        return res


JOBICY_GEOS = {"anywhere", "apac", "emea", "united-arab-emirates", "singapore", "uk", "usa", "europe", "canada",
               "germany", "australia", "israel", "japan", "malaysia", "philippines", "thailand", "vietnam"}


class JobicyAdapter:
    kind = "jobicy"
    label = "Jobicy remote jobs (public API; credited, apply links go to Jobicy's job page)"
    config_help = ('{"geos": ["anywhere", "apac", "emea", "united-arab-emirates"], "industry": "engineering", '
                   '"queries": ["all"], "title_keywords": ["software engineer", "backend", "developer"], '
                   '"exclude_keywords": ["senior", "staff", "lead", "manager"], "regions": ["remote", "india", "dubai"], '
                   '"max_results": 60}')
    URL = "https://jobicy.com/api/v2/remote-jobs"

    def validate_config(self, config: dict) -> dict:
        industry = clean_ws(str(config.get("industry") or "engineering")).lower()
        if not re.fullmatch(r"[a-z-]{2,40}", industry):
            raise ValueError("industry: a Jobicy industry slug, e.g. engineering")
        cfg = {**config, "queries": config.get("queries") or ["all"]}
        return {**_base_config(cfg, 60, 200), "industry": industry,
                "geos": _list(config, "geos", ["anywhere", "apac", "emea", "united-arab-emirates"], JOBICY_GEOS),
                "count": _limit(config, "count", 50, 100),
                "regions": _regions(config) or ["remote", "india", "dubai"]}

    def _item(self, j: dict, company_name: str | None) -> JobData:
        geo = clean_ws(re.sub(r"\s*,\s*", ", ", str(j.get("jobGeo") or "")))
        jt = j.get("jobType")
        lo, hi = j.get("annualSalaryMin") or j.get("salaryMin"), j.get("annualSalaryMax") or j.get("salaryMax")
        return _job(j.get("jobDescription") or j.get("jobExcerpt") or "", title=clean_ws(j.get("jobTitle") or ""),
                    company_name=company_name or j.get("companyName"),
                    location=f"Remote — {geo or 'Anywhere'}", remote_type="remote",
                    posted_at=parse_dt(j.get("pubDate")), source_url=j.get("url"), apply_url=j.get("url"),
                    external_id=str(j.get("id") or j.get("url")),
                    employment_type=(jt[0] if isinstance(jt, list) and jt else jt if isinstance(jt, str) else None),
                    salary_min=int(lo) if isinstance(lo, (int, float)) else None,
                    salary_max=int(hi) if isinstance(hi, (int, float)) else None,
                    salary_currency=j.get("salaryCurrency") if lo or hi else None,
                    raw={"board": "jobicy", "geo": geo, "level": j.get("jobLevel"), "credit": "https://jobicy.com"})

    def fetch(self, http, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        items: dict[str, JobData] = {}
        seen = 0
        for geo in cfg["geos"]:
            data = http.get_json(self.URL, params={"count": cfg["count"], "geo": geo, "industry": cfg["industry"]})
            if isinstance(data, dict) and data.get("success") is False:
                return FetchResult(errors=[f"Jobicy refused geo={geo}: {str(data.get('error'))[:200]}"], method="api")
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list):
                return FetchResult(errors=["Unexpected Jobicy response shape"], method="api")
            seen += len(jobs)
            for j in jobs:
                if not (isinstance(j, dict) and j.get("jobTitle") and j.get("url")) or not title_wanted(cfg, j["jobTitle"]):
                    continue
                job = self._item(j, company_name)
                if location_wanted(cfg, job.location, "remote", job.title):
                    items.setdefault(job.external_id, job)
        return _result(list(items.values()), seen, cfg, "api")
