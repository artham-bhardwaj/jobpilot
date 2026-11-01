"""Remote-job feeds published for reuse, added for the Search agent.

Checked 2026-10-24 (robots.txt and the providers' own notes):

- Remote OK: ``https://remoteok.com/api`` (robots.txt allows ``/`` for every agent; ``?action=get_jobs`` is
  disallowed and not used). Its API terms ask that Remote OK is named as the source and linked back: each job keeps
  its Remote OK URL as source and apply link, and the source is named Remote OK.
- Working Nomads: ``https://www.workingnomads.com/api/exposed_jobs/`` (robots.txt disallows nothing).
- We Work Remotely: the public category RSS feeds, e.g. ``/categories/remote-programming-jobs.rss`` (robots.txt
  allows ``/``; feeds are published for syndication).

Each returns the newest postings only, so a job missing from one run is never expired because of it. Postings
outside your regions ("USA only", "Europe only") are dropped before anything is stored.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from jobpilot.services.text_utils import clean_ws
from jobpilot.sources.base import FetchResult, JobData, parse_dt
from jobpilot.sources.careers import _base_config, _job, _regions, _result, location_wanted, title_wanted

REGIONS_DEFAULT = ["remote", "india", "dubai"]


def _rss_date(value: str):
    """RSS dates are RFC 2822 ("Tue, 06 Oct 2026 07:31:05 +0000"); stored as naive UTC like every other date."""
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return parse_dt(value)
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _remote_location(where: str | None) -> str:
    where = clean_ws(where or "")
    return "Remote — " + (where or "Worldwide")


class RemoteOKAdapter:
    kind = "remoteok"
    label = "Remote OK (public API; credited, apply links go to Remote OK's job page)"
    config_help = ('{"title_keywords": ["software engineer", "backend", "developer"], "exclude_keywords": ["senior", '
                   '"staff", "lead", "manager"], "regions": ["remote", "india", "dubai"], "max_results": 60}')
    URL = "https://remoteok.com/api"

    def validate_config(self, config: dict) -> dict:
        cfg = {**config, "queries": config.get("queries") or ["all"]}
        return {**_base_config(cfg, 60, 200), "regions": _regions(config) or REGIONS_DEFAULT}

    def _item(self, j: dict, company_name: str | None) -> JobData:
        url = j.get("url") or j.get("apply_url")
        lo, hi = j.get("salary_min"), j.get("salary_max")
        lo = int(lo) if isinstance(lo, (int, float)) and lo > 0 else None
        hi = int(hi) if isinstance(hi, (int, float)) and hi > 0 else None
        return _job(j.get("description") or "", title=clean_ws(j.get("position") or ""),
                    company_name=company_name or j.get("company"), location=_remote_location(j.get("location")),
                    remote_type="remote", posted_at=parse_dt(j.get("date")), source_url=url, apply_url=url,
                    external_id=str(j.get("id") or url), salary_min=lo, salary_max=hi,
                    salary_currency="USD" if lo or hi else None, salary_period="year" if lo or hi else None,
                    raw={"board": "remoteok", "tags": (j.get("tags") or [])[:12], "credit": "https://remoteok.com"})

    def fetch(self, http, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        data = http.get_json(self.URL)
        if not isinstance(data, list):
            return FetchResult(errors=["Unexpected Remote OK response shape"], method="api")
        rows = [j for j in data if isinstance(j, dict) and j.get("position") and (j.get("url") or j.get("apply_url"))]
        items = {}
        for j in rows:
            if not title_wanted(cfg, j["position"]):
                continue
            job = self._item(j, company_name)
            if location_wanted(cfg, job.location, "remote", job.title):
                items.setdefault(job.external_id, job)
        res = _result(list(items.values()), len(rows), cfg, "api")
        res.notes.append("Remote OK is credited as the source; apply links go to its job page")
        return res


class WorkingNomadsAdapter:
    kind = "workingnomads"
    label = "Working Nomads (public API, remote jobs)"
    config_help = ('{"categories": ["Development"], "title_keywords": ["software engineer", "backend", "developer"], '
                   '"exclude_keywords": ["senior", "staff", "lead", "manager"], "regions": ["remote", "india", "dubai"], '
                   '"max_results": 60}')
    URL = "https://www.workingnomads.com/api/exposed_jobs/"

    def validate_config(self, config: dict) -> dict:
        cfg = {**config, "queries": config.get("queries") or ["all"]}
        cats = config.get("categories") or ["Development"]
        if isinstance(cats, str):
            cats = [c for c in re.split(r"[,\n]", cats) if c.strip()]
        return {**_base_config(cfg, 60, 200), "regions": _regions(config) or REGIONS_DEFAULT,
                "categories": [clean_ws(str(c))[:40] for c in cats][:6]}

    def fetch(self, http, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        data = http.get_json(self.URL)
        if not isinstance(data, list):
            return FetchResult(errors=["Unexpected Working Nomads response shape"], method="api")
        wanted = {c.lower() for c in cfg["categories"]}
        rows = [j for j in data if isinstance(j, dict) and j.get("title") and j.get("url")]
        items = {}
        for j in rows:
            if wanted and (j.get("category_name") or "").lower() not in wanted:
                continue
            if not title_wanted(cfg, j["title"]):
                continue
            job = _job(j.get("description") or "", title=clean_ws(j["title"]),
                       company_name=company_name or j.get("company_name"), location=_remote_location(j.get("location")),
                       remote_type="remote", posted_at=parse_dt(j.get("pub_date")), source_url=j["url"],
                       apply_url=j["url"], external_id=j["url"],
                       raw={"board": "workingnomads", "tags": (j.get("tags") or "")[:200]})
            if location_wanted(cfg, job.location, "remote", job.title):
                items.setdefault(job.external_id, job)
        return _result(list(items.values()), len(rows), cfg, "api")


class WeWorkRemotelyAdapter:
    kind = "weworkremotely"
    label = "We Work Remotely (public RSS feeds)"
    config_help = ('{"feeds": ["remote-programming-jobs", "remote-back-end-programming-jobs", '
                   '"remote-full-stack-programming-jobs"], "title_keywords": ["engineer", "developer"], '
                   '"exclude_keywords": ["senior", "staff", "lead", "manager"], "regions": ["remote", "india", "dubai"], '
                   '"max_results": 60}')
    BASE = "https://weworkremotely.com/categories/{}.rss"

    def validate_config(self, config: dict) -> dict:
        cfg = {**config, "queries": config.get("queries") or ["all"]}
        feeds = config.get("feeds") or ["remote-programming-jobs", "remote-back-end-programming-jobs",
                                        "remote-full-stack-programming-jobs"]
        if isinstance(feeds, str):
            feeds = [f for f in re.split(r"[,\n]", feeds) if f.strip()]
        feeds = [f.strip().lower() for f in feeds][:6]
        if any(not re.fullmatch(r"[a-z0-9-]{3,80}", f) for f in feeds):
            raise ValueError("feeds: We Work Remotely category slugs, e.g. remote-programming-jobs")
        return {**_base_config(cfg, 60, 200), "regions": _regions(config) or REGIONS_DEFAULT, "feeds": feeds}

    @staticmethod
    def parse(xml: str) -> list[dict]:
        """Items of one RSS feed. html.parser treats <link> as empty, so the <guid> (the same URL) is used."""
        import warnings

        from bs4 import XMLParsedAsHTMLWarning

        with warnings.catch_warnings():  # html.parser on RSS is deliberate: no external XML parser, no entity expansion
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(xml, "html.parser")
        out = []
        for item in soup.find_all("item"):
            def text(tag: str) -> str:
                el = item.find(tag)
                return el.get_text(" ", strip=False).strip() if el else ""
            out.append({"title": text("title"), "region": text("region"), "guid": text("guid"),
                        "pubdate": text("pubdate"), "description": text("description"), "category": text("category")})
        return out

    def fetch(self, http, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        items: dict[str, JobData] = {}
        seen = 0
        for feed in cfg["feeds"]:
            r = http.get(self.BASE.format(feed), accept="application/rss+xml, application/xml, text/xml")
            for it in self.parse(r.text):
                seen += 1
                url = it["guid"]
                if not (it["title"] and url.startswith("https://weworkremotely.com/")):
                    continue
                company, _, title = it["title"].partition(": ")
                title = clean_ws(title or company)
                if not title_wanted(cfg, title):
                    continue
                job = _job(it["description"], title=title, company_name=company_name or clean_ws(company) or None,
                           location=_remote_location(it["region"]), remote_type="remote",
                           posted_at=_rss_date(it["pubdate"]), source_url=url, apply_url=url, external_id=url,
                           raw={"board": "weworkremotely", "category": it["category"]})
                if location_wanted(cfg, job.location, "remote", job.title):
                    items.setdefault(url, job)
        return _result(list(items.values()), seen, cfg, "rss")
