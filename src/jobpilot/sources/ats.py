"""Adapters for public job-board APIs published by ATS vendors.

These are the vendors' documented, unauthenticated *public posting* endpoints
intended for displaying a company's openings. Each source covers exactly one
company board; JobPilot does not crawl the web.

- Greenhouse Job Board API: https://boards-api.greenhouse.io/v1/boards/{token}/jobs
- Lever Postings API:       https://api.lever.co/v0/postings/{site}
- Ashby Job Posting API:    https://api.ashbyhq.com/posting-api/job-board/{board}
"""

from __future__ import annotations

import html
import re

from jobpilot.services.text_utils import html_to_text
from jobpilot.sources.base import FetchResult, JobData, parse_dt
from jobpilot.sources.http import PoliteHttp

_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _token(config: dict, key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"'{key}' must be the board identifier (letters, digits, - _ .)")
    return value


class GreenhouseAdapter:
    kind = "greenhouse"
    label = "Greenhouse job board"
    config_help = '{"board_token": "examplecompany"}  — from boards.greenhouse.io/<board_token>'
    BASE = "https://boards-api.greenhouse.io/v1/boards"

    def validate_config(self, config: dict) -> dict:
        return {"board_token": _token(config, "board_token")}

    def _item(self, j: dict, company_name: str | None) -> JobData:
        content = html.unescape(j.get("content") or "")
        loc = (j.get("location") or {}).get("name")
        depts = ", ".join(d.get("name", "") for d in j.get("departments") or [] if d.get("name"))
        return JobData(
            title=j.get("title") or "",
            company_name=company_name or j.get("company_name"),
            location=loc,
            description_text=html_to_text(content),
            description_raw=content or None,
            posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
            source_url=j.get("absolute_url"),
            apply_url=j.get("absolute_url"),
            external_id=str(j.get("id")) if j.get("id") is not None else None,
            department=depts or None,
            raw={"id": j.get("id"), "updated_at": j.get("updated_at"), "requisition_id": j.get("requisition_id")},
        )

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        token = self.validate_config(config)["board_token"]
        data = http.get_json(f"{self.BASE}/{token}/jobs", params={"content": "true"})
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if jobs is None:
            return FetchResult(errors=["Unexpected Greenhouse response shape"])
        return FetchResult(items=[self._item(j, company_name) for j in jobs if j.get("title")], full_snapshot=True)

    def fetch_one(self, http: PoliteHttp, token: str, job_id: str, company_name: str | None) -> JobData:
        j = http.get_json(f"{self.BASE}/{_token({'t': token}, 't')}/jobs/{int(job_id)}")
        return self._item(j, company_name)


class LeverAdapter:
    kind = "lever"
    label = "Lever job board"
    config_help = '{"site": "examplecompany"}  — from jobs.lever.co/<site>'
    BASE = "https://api.lever.co/v0/postings"

    def validate_config(self, config: dict) -> dict:
        return {"site": _token(config, "site")}

    def _item(self, p: dict, company_name: str | None) -> JobData:
        cats = p.get("categories") or {}
        parts = [p.get("descriptionPlain") or html_to_text(p.get("description"))]
        for lst in p.get("lists") or []:
            parts.append(f"{lst.get('text', '')}:")
            parts.append(html_to_text(lst.get("content")))
        parts.append(p.get("additionalPlain") or html_to_text(p.get("additional")))
        sal = p.get("salaryRange") or {}
        workplace = (p.get("workplaceType") or "").lower()
        return JobData(
            title=p.get("text") or "",
            company_name=company_name,
            location=cats.get("location") or ", ".join(cats.get("allLocations") or []) or None,
            remote_type=workplace if workplace in ("remote", "hybrid", "onsite") else None,
            description_text="\n".join(x for x in parts if x).strip(),
            description_raw="\n".join(x for x in [p.get("description") or p.get("descriptionPlain") or ""]
                                      + [f"<h3>{lst.get('text', '')}</h3>{lst.get('content', '')}" for lst in p.get("lists") or []]
                                      + [p.get("additional") or p.get("additionalPlain") or ""] if x) or None,
            posted_at=parse_dt(p.get("createdAt")),
            source_url=p.get("hostedUrl"),
            apply_url=p.get("applyUrl") or p.get("hostedUrl"),
            external_id=p.get("id"),
            employment_type=cats.get("commitment"),
            department=cats.get("team") or cats.get("department"),
            salary_min=sal.get("min"),
            salary_max=sal.get("max"),
            salary_currency=sal.get("currency"),
            salary_period=sal.get("interval"),
            raw={"id": p.get("id"), "categories": cats},
        )

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        site = self.validate_config(config)["site"]
        items: list[JobData] = []
        skip, limit = 0, 100
        while True:
            data = http.get_json(f"{self.BASE}/{site}", params={"mode": "json", "skip": skip, "limit": limit})
            if not isinstance(data, list):
                return FetchResult(items=items, errors=["Unexpected Lever response shape"])
            items.extend(self._item(p, company_name) for p in data if p.get("text"))
            if len(data) < limit or skip > 2000:
                break
            skip += limit
        return FetchResult(items=items, full_snapshot=True)

    def fetch_one(self, http: PoliteHttp, site: str, posting_id: str, company_name: str | None) -> JobData:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", posting_id):
            raise ValueError("Invalid Lever posting id")
        p = http.get_json(f"{self.BASE}/{_token({'s': site}, 's')}/{posting_id}", params={"mode": "json"})
        return self._item(p, company_name)


class AshbyAdapter:
    kind = "ashby"
    label = "Ashby job board"
    config_help = '{"board": "examplecompany"}  — from jobs.ashbyhq.com/<board>'
    BASE = "https://api.ashbyhq.com/posting-api/job-board"

    def validate_config(self, config: dict) -> dict:
        return {"board": _token(config, "board")}

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        board = self.validate_config(config)["board"]
        data = http.get_json(f"{self.BASE}/{board}", params={"includeCompensation": "true"})
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if jobs is None:
            return FetchResult(errors=["Unexpected Ashby response shape"])
        items = []
        for j in jobs:
            if not j.get("title") or j.get("isListed") is False:
                continue
            workplace = (j.get("workplaceType") or "").lower().replace("-", "")
            remote = "remote" if j.get("isRemote") or workplace == "remote" else (
                workplace if workplace in ("hybrid", "onsite") else None)
            items.append(JobData(
                title=j["title"],
                company_name=company_name,
                location=j.get("location"),
                remote_type=remote,
                description_text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                description_raw=j.get("descriptionHtml") or j.get("descriptionPlain"),
                posted_at=parse_dt(j.get("publishedAt")),
                source_url=j.get("jobUrl"),
                apply_url=j.get("applyUrl") or j.get("jobUrl"),
                external_id=j.get("id"),
                employment_type=j.get("employmentType"),
                department=j.get("department") or j.get("team"),
                raw={"id": j.get("id"), "compensation": (j.get("compensation") or {}).get("compensationTierSummary")},
            ))
        return FetchResult(items=items, full_snapshot=True)
