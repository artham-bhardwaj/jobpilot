"""Adapters for employers' own career-site job search, where the site publishes it and robots.txt permits it.

Each adapter reads one employer's public job search (the same data its careers page shows), never an account
area, and keeps only postings whose titles match your keywords. robots.txt (RFC 9309) and per-host rate limits
apply to every request (``PoliteHttp``). Checked 2026-10-18:

- Amazon:    ``https://www.amazon.jobs/en/search.json`` (robots.txt allows; only ``/internal`` is disallowed).
- Microsoft: ``https://apply.careers.microsoft.com/api/pcsx/search`` and ``…/position_details``
  (robots.txt: ``Disallow: /`` with ``Allow: /api/pcsx``, the longer rule wins).
- Google:    ``https://www.google.com/about/careers/applications/jobs/results`` first page only (robots.txt
  disallows ``?page=`` pagination, so further pages are never requested). The jobs are read from the data the
  page embeds for its own rendering; if Google changes that structure the run fails loudly instead of guessing.
- Atlassian: ``https://www.atlassian.com/endpoint/careers/listings`` (the full listing its careers page loads).
- Apple:     ``https://jobs.apple.com/en-in/search`` and ``…/details/<id>/<slug>`` (jobs.apple.com has no
  robots.txt — it answers 404, which RFC 9309 treats as no restrictions). Jobs are read from the data each page
  embeds for its own rendering; a changed structure fails the run instead of guessing.
- Not covered: Meta (its careers site is an internal GraphQL app and its terms forbid automated collection) and
  Netflix (no roles in India when checked).

Searches return a subset of the employer's openings, so a job missing from one run is never expired because of
it (``full_snapshot=False``), except Atlassian, whose endpoint is the complete listing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from jobpilot.services.text_utils import clean_ws, description_to_text
from jobpilot.sources.base import FetchResult, JobData, parse_dt
from jobpilot.sources.http import FetchError, PoliteHttp

MAX_RESULTS = 200


def _words(config: dict, key: str, required: bool = False) -> list[str]:
    from jobpilot.sources.pages import _slug_text

    v = config.get(key) or []
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    out = [_slug_text(str(x)) for x in v if _slug_text(str(x))]
    if required and not out:
        raise ValueError(f"{key}: at least one keyword is required")
    return out


def _queries(config: dict) -> list[str]:
    v = config.get("queries") or []
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    out = [clean_ws(str(x))[:100] for x in v if clean_ws(str(x))]
    if not out or len(out) > 10:
        raise ValueError("queries: between 1 and 10 search phrases, e.g. [\"software engineer\"]")
    return out


def _limit(config: dict, key: str, default: int, top: int = MAX_RESULTS) -> int:
    try:
        n = int(config.get(key) or default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not 1 <= n <= top:
        raise ValueError(f"{key} must be between 1 and {top}")
    return n


def _base_config(config: dict, max_default: int, max_top: int = MAX_RESULTS) -> dict:
    return {"queries": _queries(config), "title_keywords": _words(config, "title_keywords", True),
            "exclude_keywords": _words(config, "exclude_keywords"),
            "max_results": _limit(config, "max_results", max_default, max_top)}


def _regions(config: dict) -> list[str]:
    """Optional ``regions``: keep postings in India, remote (open to India / the UAE) or Dubai / UAE."""
    from jobpilot.services.regions import REGIONS

    v = config.get("regions") or []
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    out = [str(x).strip().lower() for x in v if str(x).strip()]
    bad = [x for x in out if x not in REGIONS]
    if bad:
        raise ValueError(f"regions: unknown {bad}; use any of {list(REGIONS)}")
    return list(dict.fromkeys(out))


def filter_config(config: dict) -> dict:
    """Optional keyword filters for whole-board sources (Greenhouse, Lever, Ashby); empty = keep every posting."""
    return {k: v for k, v in {"title_keywords": _words(config, "title_keywords"),
                              "location_keywords": _words(config, "location_keywords"),
                              "regions": _regions(config),
                              "exclude_keywords": _words(config, "exclude_keywords")}.items() if v}


def location_wanted(cfg: dict, location: str | None, remote_type: str | None = None, title: str | None = None) -> bool:
    """No location filter = keep. Otherwise keep a posting whose location matches a keyword OR is in one of the
    configured regions (e.g. "Remote - India", "Dubai, United Arab Emirates")."""
    from jobpilot.services.regions import classify
    from jobpilot.sources.pages import _has_phrase, _slug_text

    if not cfg.get("location_keywords") and not cfg.get("regions"):
        return True
    loc = _slug_text(location or "")
    if any(_has_phrase(loc, k) for k in cfg.get("location_keywords") or []):
        return True
    return bool(set(cfg.get("regions") or []) & set(classify(location, remote_type, title)))


def keep_posting(cfg: dict, title: str, location: str | None, remote_type: str | None = None) -> bool:
    from jobpilot.sources.pages import _has_phrase, _slug_text

    text = _slug_text(title or "")
    if cfg.get("title_keywords") and not any(_has_phrase(text, k) for k in cfg["title_keywords"]):
        return False
    if not location_wanted(cfg, location, remote_type, title):
        return False
    return not any(_has_phrase(text, k) for k in cfg.get("exclude_keywords") or [])


def apply_filters(result: FetchResult, cfg: dict) -> FetchResult:
    if not cfg:
        return result
    before = len(result.items)
    result.items = [j for j in result.items if keep_posting(cfg, j.title, j.location, j.remote_type)]
    result.counts = {**(result.counts or {}), "listed": before, "matched": len(result.items)}
    result.notes.append(f"board lists {before} posting(s); {len(result.items)} matched your keywords")
    return result


def title_wanted(cfg: dict, title: str) -> bool:
    from jobpilot.sources.pages import _has_phrase, _slug_text

    text = _slug_text(title)
    return any(_has_phrase(text, k) for k in cfg["title_keywords"]) and \
        not any(_has_phrase(text, k) for k in cfg["exclude_keywords"])


def _sections(*parts: tuple[str, str | None]) -> str:
    """Join (heading, html) parts into one HTML description with real headings (the parser splits on them)."""
    out = []
    for heading, body in parts:
        if body and body.strip():
            out.append(f"<h3>{heading}</h3>\n{body}" if heading else body)
    return "\n".join(out)


def _job(raw_html: str, **kw: Any) -> JobData:
    return JobData(description_raw=raw_html or None, description_text=description_to_text(raw_html), **kw)


def _result(items: list[JobData], seen: int, cfg: dict, method: str, full: bool = False, errors=None) -> FetchResult:
    kept = items[:cfg["max_results"]]
    res = FetchResult(items=kept, full_snapshot=full and len(kept) == len(items), method=method, errors=errors or [])
    res.counts = {"listed": seen, "matched": len(items), "read": len(kept)}
    res.notes.append(f"search returned {seen} posting(s); {len(items)} matched your title keywords; kept {len(kept)}"
                     + (f" (max_results {cfg['max_results']})" if len(items) > len(kept) else ""))
    return res


# ---- Amazon -------------------------------------------------------------------------------------------------------

_COUNTRIES = {"IND": "India", "USA": "United States", "GBR": "United Kingdom", "IRL": "Ireland", "CAN": "Canada",
              "DEU": "Germany", "SGP": "Singapore", "JPN": "Japan", "AUS": "Australia", "ARE": "United Arab Emirates"}


class AmazonJobsAdapter:
    kind = "amazon_jobs"
    label = "Amazon jobs (amazon.jobs search)"
    config_help = ('{"queries": ["software development engineer"], "country": "IND", '
                   '"title_keywords": ["software development engineer", "software engineer"], '
                   '"exclude_keywords": ["senior", "sr", "iii", "manager", "principal"], "max_results": 100}')
    URL = "https://www.amazon.jobs/en/search.json"
    PAGE = 100

    def validate_config(self, config: dict) -> dict:
        cfg = _base_config(config, 100)
        country = str(config.get("country") or "").strip().upper()
        if country and not re.fullmatch(r"[A-Z]{3}", country):
            raise ValueError("country must be a 3-letter country code, e.g. IND")
        return {**cfg, "country": country}

    @staticmethod
    def _location(j: dict) -> str | None:
        country = _COUNTRIES.get(str(j.get("country_code") or ""), j.get("country_code"))
        parts = [j.get("city"), country]
        return ", ".join(str(p) for p in parts if p) or j.get("normalized_location") or j.get("location")

    def _item(self, j: dict, company_name: str | None) -> JobData:
        try:
            posted = datetime.strptime(str(j.get("posted_date") or "").strip(), "%B %d, %Y")
        except ValueError:
            posted = None
        raw_html = _sections(("", j.get("description")), ("Basic Qualifications", j.get("basic_qualifications")),
                             ("Preferred Qualifications", j.get("preferred_qualifications")))
        path = str(j.get("job_path") or "")
        url = f"https://www.amazon.jobs{path}" if path.startswith("/") else None
        return _job(raw_html, title=clean_ws(j.get("title") or ""), company_name=company_name or "Amazon",
                    location=self._location(j), posted_at=posted, source_url=url,
                    apply_url=j.get("url_next_step") or url, external_id=str(j.get("id_icims") or j.get("id") or "") or None,
                    employment_type=j.get("job_schedule_type"), department=j.get("job_category"),
                    raw={"id": j.get("id"), "id_icims": j.get("id_icims"), "business": j.get("company_name"),
                         "team": (j.get("team") or {}).get("label") if isinstance(j.get("team"), dict) else None})

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        items: dict[str, JobData] = {}
        seen = 0
        for q in cfg["queries"]:
            offset = 0
            while offset < cfg["max_results"] * 3:  # titles are filtered locally, so read a little past the cap
                params = {"base_query": q, "result_limit": self.PAGE, "offset": offset, "sort": "recent"}
                if cfg["country"]:
                    params["country"] = cfg["country"]
                data = http.get_json(self.URL, params=params)
                jobs = data.get("jobs") if isinstance(data, dict) else None
                if jobs is None:
                    return FetchResult(errors=["Unexpected amazon.jobs response shape"], method="api")
                seen += len(jobs)
                for j in jobs:
                    if j.get("title") and title_wanted(cfg, j["title"]):
                        job = self._item(j, company_name)
                        items.setdefault(job.external_id or job.source_url or job.title, job)
                if len(jobs) < self.PAGE or offset + self.PAGE >= int(data.get("hits") or 0):
                    break
                offset += self.PAGE
        return _result(list(items.values()), seen, cfg, "api")


# ---- Microsoft ----------------------------------------------------------------------------------------------------

class MicrosoftCareersAdapter:
    kind = "microsoft_careers"
    label = "Microsoft careers (apply.careers.microsoft.com)"
    config_help = ('{"queries": ["software engineer"], "location": "India", "seniority": ["Entry", "Mid-Level"], '
                   '"title_keywords": ["software engineer"], "exclude_keywords": ["senior", "principal", "manager"], '
                   '"max_results": 30}')
    BASE = "https://apply.careers.microsoft.com/api/pcsx"
    PAGE = 10  # the API returns at most 10 per request

    def validate_config(self, config: dict) -> dict:
        cfg = _base_config(config, 30, 60)
        seniority = config.get("seniority") or []
        if isinstance(seniority, str):
            seniority = [s for s in re.split(r"[,\n]", seniority)]
        allowed = {"Entry", "Mid-Level", "Senior", "Intern"}
        seniority = [s.strip() for s in seniority if s.strip()]
        if any(s not in allowed for s in seniority):
            raise ValueError(f"seniority values must be among {sorted(allowed)}")
        return {**cfg, "location": clean_ws(str(config.get("location") or ""))[:80], "seniority": seniority}

    def _search(self, http: PoliteHttp, cfg: dict, q: str, seniority: str | None) -> tuple[list[dict], int]:
        positions: list[dict] = []
        start = 0
        while start < 100:
            params: dict[str, Any] = {"domain": "microsoft.com", "query": q, "location": cfg["location"],
                                      "start": start, "num": self.PAGE, "sort_by": "timestamp"}
            if seniority:
                params["filter_seniority"] = seniority
            data = (http.get_json(f"{self.BASE}/search", params=params) or {}).get("data")
            if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
                raise FetchError("Unexpected Microsoft careers response shape")
            positions.extend(data["positions"])
            if len(data["positions"]) < self.PAGE or start + self.PAGE >= int(data.get("count") or 0):
                break
            start += self.PAGE
        return positions, len(positions)

    def _item(self, d: dict, company_name: str | None) -> JobData:
        locs = d.get("locations") or []
        location = None
        if locs:  # "India, Karnataka, Bangalore" -> "Bangalore, Karnataka, India"
            location = ", ".join(reversed([p.strip() for p in str(locs[0]).split(",")]))
        work = str(d.get("workLocationOption") or "").lower()
        emp = d.get("efcustomTextEmploymentType")
        return _job(d.get("jobDescription") or "", title=clean_ws(d.get("name") or ""),
                    company_name=company_name or "Microsoft", location=location,
                    remote_type={"onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}.get(work),
                    posted_at=parse_dt(d.get("postedTs")),
                    source_url=d.get("publicUrl") or f"https://apply.careers.microsoft.com/careers/job/{d.get('id')}",
                    apply_url=d.get("publicUrl"), external_id=str(d.get("displayJobId") or d.get("id") or "") or None,
                    employment_type=(emp[0] if isinstance(emp, list) and emp else None), department=d.get("department"),
                    raw={"id": d.get("id"), "atsJobId": d.get("atsJobId"), "locations": locs[:5]})

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        wanted: dict[str, dict] = {}
        seen = 0
        for q in cfg["queries"]:
            for level in cfg["seniority"] or [None]:
                positions, n = self._search(http, cfg, q, level)
                seen += n
                for p in positions:
                    if p.get("id") and p.get("name") and title_wanted(cfg, p["name"]):
                        wanted.setdefault(str(p["id"]), p)
        items, errors = [], []
        for pid in list(wanted)[:cfg["max_results"]]:  # one detail request per kept posting
            try:
                d = (http.get_json(f"{self.BASE}/position_details",
                                   params={"position_id": pid, "domain": "microsoft.com", "hl": "en"}) or {}).get("data")
            except FetchError as exc:
                errors.append(f"position {pid}: {exc}")
                continue
            if isinstance(d, dict) and d.get("name"):
                items.append(self._item(d, company_name))
        res = _result(items, seen, cfg, "api", errors=errors)
        res.counts["matched"] = len(wanted)
        return res


# ---- Google -------------------------------------------------------------------------------------------------------

_GOOGLE_DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel: \{\}\}\);", re.S)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


class GoogleCareersAdapter:
    kind = "google_careers"
    label = "Google careers (first results page per query)"
    config_help = ('{"queries": ["software engineer", "backend"], "location": "India", "target_level": ["EARLY"], '
                   '"title_keywords": ["software engineer", "software developer"], "exclude_keywords": ["senior", '
                   '"staff", "manager"], "max_results": 40}')
    URL = "https://www.google.com/about/careers/applications/jobs/results"

    def validate_config(self, config: dict) -> dict:
        cfg = _base_config(config, 40, 100)
        levels = config.get("target_level") or []
        if isinstance(levels, str):
            levels = re.split(r"[,\n]", levels)
        levels = [str(x).strip().upper() for x in levels if str(x).strip()]
        if any(x not in {"INTERN_AND_APPRENTICE", "EARLY", "MID", "ADVANCED", "DIRECTOR_PLUS"} for x in levels):
            raise ValueError("target_level values: INTERN_AND_APPRENTICE, EARLY, MID, ADVANCED, DIRECTOR_PLUS")
        return {**cfg, "location": clean_ws(str(config.get("location") or ""))[:80], "target_level": levels}

    @staticmethod
    def parse(page: str) -> list[list]:
        m = _GOOGLE_DATA.search(page)
        if not m:
            raise FetchError("Google careers page structure not recognised (no embedded results); nothing imported")
        try:
            data = json.loads(m.group(1))
            jobs = data[0] or []
        except (ValueError, IndexError, TypeError) as exc:
            raise FetchError("Google careers embedded results could not be read; nothing imported") from exc
        for j in jobs:  # verify the field positions this adapter relies on before using any of them
            if not (isinstance(j, list) and len(j) > 14 and isinstance(j[0], str) and isinstance(j[1], str)
                    and isinstance(j[9], list)):
                raise FetchError("Google careers result layout changed; nothing imported")
        return jobs

    @staticmethod
    def _text(cell: Any) -> str:
        return cell[1] if isinstance(cell, list) and len(cell) > 1 and isinstance(cell[1], str) else ""

    def _item(self, j: list, company_name: str | None) -> JobData:
        jid, title = j[0], clean_ws(j[1])
        quals = self._text(j[4])
        raw_html = _sections(("", self._text(j[10])), ("Responsibilities", self._text(j[3])), ("", quals))
        locs = [loc[0] for loc in j[9] if isinstance(loc, list) and loc and isinstance(loc[0], str)]
        posted = j[12][0] if isinstance(j[12], list) and j[12] and isinstance(j[12][0], int) else None
        url = f"{self.URL}/{jid}-{_slug(title)}"
        return _job(raw_html, title=title, company_name=company_name or (j[7] if isinstance(j[7], str) else "Google"),
                    location="; ".join(locs[:3]) or None, posted_at=parse_dt(posted), source_url=url, apply_url=url,
                    external_id=jid, raw={"id": jid, "locations": locs[:10]})

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        items: dict[str, JobData] = {}
        seen = 0
        for q in cfg["queries"]:
            params: dict[str, Any] = {"q": q}
            if cfg["location"]:
                params["location"] = cfg["location"]
            if cfg["target_level"]:
                params["target_level"] = cfg["target_level"]
            jobs = self.parse(http.get(self.URL, params=params, accept="text/html").text)
            seen += len(jobs)
            for j in jobs:
                if title_wanted(cfg, j[1]):
                    items.setdefault(j[0], self._item(j, company_name))
        res = _result(list(items.values()), seen, cfg, "html")
        res.notes.append("first results page per query only (robots.txt disallows paging)")
        return res


# ---- Atlassian ----------------------------------------------------------------------------------------------------

class AtlassianCareersAdapter:
    kind = "atlassian_careers"
    label = "Atlassian careers (complete listing)"
    config_help = ('{"queries": ["engineer"], "location_keywords": ["india"], "title_keywords": ["software engineer", '
                   '"backend"], "exclude_keywords": ["principal", "manager", "senior"], "max_results": 50}')
    URL = "https://www.atlassian.com/endpoint/careers/listings"

    def validate_config(self, config: dict) -> dict:
        cfg = {**config, "queries": config.get("queries") or ["all"]}
        return {**_base_config(cfg, 50), "location_keywords": _words(config, "location_keywords"),
                "regions": _regions(config)}

    def _item(self, j: dict, company_name: str | None) -> JobData:
        raw_html = _sections(("", j.get("overview")), ("Responsibilities", j.get("responsibilities")),
                             ("Qualifications", j.get("qualifications")))
        post = j.get("portalJobPost") or {}
        locs = [clean_ws(str(x)) for x in j.get("locations") or []]
        remote = "remote" if locs and all(x.lower().startswith("remote") for x in locs) else None
        return _job(raw_html, title=clean_ws(j.get("title") or ""), company_name=company_name or "Atlassian",
                    location="; ".join(locs[:3]) or None, remote_type=remote, source_url=post.get("portalUrl"),
                    apply_url=j.get("applyUrl") or post.get("portalUrl"), external_id=str(j.get("id") or "") or None,
                    employment_type=j.get("type"), department=j.get("category"),
                    posted_at=parse_dt(str(post.get("updatedDate") or "")[:10] or None),
                    raw={"id": j.get("id"), "portal": "icims", "category": j.get("category")})

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        data = http.get_json(self.URL)
        if not isinstance(data, list):
            return FetchResult(errors=["Unexpected Atlassian listing shape"], method="api")
        items = []
        for j in data:
            if not (j.get("title") and title_wanted(cfg, j["title"])):
                continue
            if not location_wanted(cfg, "; ".join(str(x) for x in j.get("locations") or []), None, j["title"]):
                continue
            items.append(self._item(j, company_name))
        # The endpoint is the complete listing, so jobs absent from it are counted missing (expired only after
        # several consecutive misses, see ingest()). Changing the keywords counts filtered-out jobs as missing too.
        return _result(items, len(data), cfg, "api", full=True)


# ---- Apple --------------------------------------------------------------------------------------------------------

_APPLE_DATA = re.compile(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);", re.S)


def _lines_html(text: str | None) -> str | None:
    import html as _html

    rows = [clean_ws(x) for x in (text or "").split("\n") if clean_ws(x)]
    return "<ul>" + "".join(f"<li>{_html.escape(r)}</li>" for r in rows) + "</ul>" if rows else None


class AppleJobsAdapter:
    kind = "apple_jobs"
    label = "Apple jobs (jobs.apple.com search, with each matching job's details)"
    config_help = ('{"queries": ["software engineer"], "location": "india-INDC", "title_keywords": ["software engineer", '
                   '"backend"], "exclude_keywords": ["senior", "manager"], "max_pages": 3, "max_results": 40}')
    BASE = "https://jobs.apple.com/en-in"

    def validate_config(self, config: dict) -> dict:
        cfg = _base_config(config, 40, 100)
        loc = clean_ws(str(config.get("location") or "india-INDC"))[:60]
        if not re.fullmatch(r"[A-Za-z0-9-]+", loc):
            raise ValueError("location: Apple's location filter, e.g. india-INDC")
        return {**cfg, "location": loc, "max_pages": _limit(config, "max_pages", 3, 10)}

    @staticmethod
    def parse(page: str, section: str) -> dict:
        """The data the page embeds for its own rendering (loaderData.<section>)."""
        m = _APPLE_DATA.search(page)
        if not m:
            raise FetchError("Apple jobs page structure not recognised (no embedded data); nothing imported")
        try:
            return json.loads(json.loads(m.group(1)))["loaderData"][section]
        except (ValueError, KeyError, TypeError) as exc:
            raise FetchError("Apple jobs embedded data could not be read; nothing imported") from exc

    def _search(self, http: PoliteHttp, cfg: dict, q: str, page: int) -> tuple[list[dict], int]:
        data = self.parse(http.get(f"{self.BASE}/search", params={"search": q, "location": cfg["location"],
                                                                   "page": page}, accept="text/html").text, "search")
        rows = data.get("searchResults")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("postingTitle"), str)
                                             or not r.get("id") for r in rows):
            raise FetchError("Apple jobs result layout changed; nothing imported")
        return rows, int(data.get("totalRecords") or 0)

    def _item(self, http: PoliteHttp, r: dict, company_name: str | None, errors: list) -> JobData:
        title = clean_ws(r["postingTitle"])
        url = f"{self.BASE}/details/{r['id']}/{r.get('transformedPostingTitle') or _slug(title)}"
        locs = list(dict.fromkeys(x.get("name") for x in r.get("locations") or [] if isinstance(x, dict) and x.get("name")))
        details: dict = {}
        try:
            details = self.parse(http.get(url, accept="text/html").text, "jobDetails").get("jobsData") or {}
        except FetchError as exc:
            errors.append(f"{title}: details not read ({exc}); the search summary is used")
        dlocs = [", ".join(x for x in (loc.get("city"), loc.get("countryName")) if x)
                 for loc in details.get("locations") or [] if isinstance(loc, dict)]
        raw_html = _sections(("", _lines_html(details.get("jobSummary") or r.get("jobSummary"))),
                             ("Description", _lines_html(details.get("description"))),
                             ("Minimum Qualifications", _lines_html(details.get("minimumQualifications"))),
                             ("Preferred Qualifications", _lines_html(details.get("preferredQualifications"))))
        return _job(raw_html, title=title, company_name=company_name or "Apple",
                    location="; ".join([x for x in dlocs if x][:3] or locs[:3]) or None,
                    posted_at=parse_dt(details.get("postDateInGMT") or r.get("postDateInGMT")), source_url=url,
                    apply_url=url, external_id=str(r["id"]),
                    raw={"id": r["id"], "team": (r.get("team") or {}).get("teamName"), "locations": locs[:10]})

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        wanted: dict[str, dict] = {}
        seen = 0
        for q in cfg["queries"]:
            for page in range(1, cfg["max_pages"] + 1):
                rows, total = self._search(http, cfg, q, page)
                seen += len(rows)
                for r in rows:
                    if title_wanted(cfg, r["postingTitle"]):
                        # One requisition is listed once per city (200677836-0321 Bengaluru, …-1052 Hyderabad):
                        # keep one job and the other cities as its locations.
                        req = str(r.get("positionId") or str(r["id"]).split("-")[0])
                        if req in wanted:
                            wanted[req]["locations"] = (wanted[req].get("locations") or []) + (r.get("locations") or [])
                        else:
                            wanted[req] = {**r, "locations": list(r.get("locations") or [])}
                if not rows or page * 20 >= total:
                    break
        errors: list[str] = []
        items = [self._item(http, r, company_name, errors) for r in list(wanted.values())[:cfg["max_results"]]]
        res = _result(items, seen, cfg, "html", errors=errors)
        res.counts["matched"] = len(wanted)
        return res
