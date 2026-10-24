"""Workday career sites: the site's own job search, then each kept posting's details.

``https://<tenant>.wd<N>.myworkdayjobs.com/<site>`` career pages load their openings from
``/wday/cxs/<tenant>/<site>/jobs`` (a JSON search, POST) and each posting from ``/wday/cxs/<tenant>/<site>/job/…``.
robots.txt on the Workday sites checked (Visa, Mastercard, Adobe; 2026-10-18) disallows neither. Compared with the
sitemap (about the 100 newest postings worldwide, descriptions flattened to one line) the search finds the site's
India software roles (Mastercard: 129 for "software engineer india" vs 8) with HTML descriptions and the official
closing date.

- Read-only: only the public search and posting endpoints are used; nothing about applying, no account area.
- Results of a search are a subset of the site's openings, so jobs missing from a run are never expired by it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from jobpilot.services.text_utils import clean_ws, description_to_text
from jobpilot.sources.base import FetchResult, JobData, parse_deadline, parse_dt
from jobpilot.sources.careers import _base_config, _regions, _words, keep_posting
from jobpilot.sources.http import FetchError, PoliteHttp

_SITE = re.compile(r"^https://(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                   r"(?P<site>[A-Za-z0-9_-]+)/?$")
PAGE = 20  # what the Workday search returns per request


class WorkdayAdapter:
    kind = "workday"
    label = "Workday career site (job search)"
    config_help = ('{"site_url": "https://<tenant>.wd5.myworkdayjobs.com/<site>", "queries": ["software engineer india"], '
                   '"title_keywords": ["software engineer"], "location_keywords": ["india"], '
                   '"exclude_keywords": ["senior"], "max_results": 40}')

    def validate_config(self, config: dict) -> dict:
        url = str(config.get("site_url") or "").strip().rstrip("/")
        m = _SITE.match(url)
        if not m:
            raise ValueError("site_url must be a Workday career site, e.g. https://acme.wd5.myworkdayjobs.com/External")
        return {**_base_config(config, 40, 60), "site_url": url, "location_keywords": _words(config, "location_keywords"),
                "regions": _regions(config), **m.groupdict()}

    @staticmethod
    def _api(cfg: dict) -> str:
        return f"https://{cfg['tenant']}.{cfg['wd']}.myworkdayjobs.com/wday/cxs/{cfg['tenant']}/{cfg['site']}"

    def _search(self, http: PoliteHttp, cfg: dict, query: str) -> tuple[list[dict], int]:
        found: list[dict] = []
        total = 0
        for offset in range(0, 200, PAGE):  # at most 10 pages per query
            data = http.post_json(f"{self._api(cfg)}/jobs",
                                  {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": query})
            posts = data.get("jobPostings") if isinstance(data, dict) else None
            if not isinstance(posts, list):
                raise FetchError("Unexpected Workday search response shape")
            total = int(data.get("total") or total or 0)
            found.extend(p for p in posts if isinstance(p, dict) and p.get("title") and p.get("externalPath"))
            if len(posts) < PAGE or offset + PAGE >= total:
                break
        return found, total

    def _item(self, info: dict, company_name: str | None, cfg: dict) -> JobData:
        html_desc = info.get("jobDescription") or ""
        posted = parse_dt(info.get("startDate"))
        deadline, problem = parse_deadline(info.get("endDate"), posted)
        raw: dict[str, Any] = {"id": info.get("id"), "jobReqId": info.get("jobReqId"), "site": cfg["site"],
                               "description_source": "workday job API (HTML)"}
        if info.get("endDate"):
            raw["end_date"] = str(info["endDate"])[:32]  # as received
            raw["deadline_source" if deadline else "deadline_ignored"] = "Workday endDate" if deadline else problem
        country = ((info.get("country") or {}).get("descriptor") if isinstance(info.get("country"), dict) else None)
        location = clean_ws(info.get("location") or "") or None
        if location and country and country.lower() not in location.lower():
            location = f"{location}, {country}"
        return JobData(title=clean_ws(info.get("title") or ""), company_name=company_name, location=location,
                       description_text=description_to_text(html_desc), description_raw=html_desc or None,
                       posted_at=posted, valid_through=deadline, source_url=info.get("externalUrl"),
                       apply_url=info.get("externalUrl"), external_id=info.get("jobReqId") or info.get("id"),
                       employment_type=info.get("timeType"), raw=raw)

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        wanted: dict[str, dict] = {}
        listed = 0
        for q in cfg["queries"]:
            posts, total = self._search(http, cfg, q)
            listed += total
            for p in posts:
                if keep_posting(cfg, p["title"], p.get("locationsText") or p["externalPath"].replace("-", " ")):
                    wanted.setdefault(p["externalPath"], p)
        items, errors = [], []
        for path in list(wanted)[:cfg["max_results"]]:
            if not path.startswith("/job/") or ".." in path:
                errors.append(f"unexpected posting path {path[:80]!r}")
                continue
            try:
                info = (http.get_json(self._api(cfg) + path) or {}).get("jobPostingInfo") or {}
            except FetchError as exc:  # e.g. 403 for a posting that was just closed: skip it, keep the rest
                errors.append(f"{path[:80]}: {exc}"[:300])
                continue
            if info.get("title") and urlsplit(info.get("externalUrl") or "").netloc.endswith("myworkdayjobs.com"):
                items.append(self._item(info, company_name, cfg))
        res = FetchResult(items=items, full_snapshot=False, errors=errors if not items else [], method="api")
        if items and errors:
            res.notes.append(f"{len(errors)} posting(s) could not be read (closed or unavailable): skipped")
        res.counts = {"listed": listed, "matched": len(wanted), "read": len(items)}
        res.notes.append(f"search found {listed} posting(s); {len(wanted)} matched your keywords; read {len(items)}"
                         + (f" (max_results {cfg['max_results']})" if len(wanted) > cfg["max_results"] else ""))
        return res
