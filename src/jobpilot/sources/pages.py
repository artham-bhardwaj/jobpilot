"""Career-page adapter (schema.org JobPosting JSON-LD), manual URLs and file imports."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from jobpilot.services.text_utils import clean_ws, html_to_text
from jobpilot.sources.ats import GreenhouseAdapter, LeverAdapter
from jobpilot.sources.base import FetchResult, JobData, parse_dt
from jobpilot.sources.http import PoliteHttp

MAX_PAGES_PER_SOURCE = 50


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _find_postings(node: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, dict):
        types = _as_list(node.get("@type"))
        if "JobPosting" in types:
            out.append(node)
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                out.extend(_find_postings(node[key]))
    elif isinstance(node, list):
        for n in node:
            out.extend(_find_postings(n))
    return out


def _location(posting: dict) -> str | None:
    parts = []
    for loc in _as_list(posting.get("jobLocation")):
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            bits = [addr.get("addressLocality"), addr.get("addressRegion"), _country(addr.get("addressCountry"))]
            parts.append(", ".join(b for b in bits if b))
        elif isinstance(addr, str):
            parts.append(addr)
    for req in _as_list(posting.get("applicantLocationRequirements")):
        if isinstance(req, dict) and req.get("name"):
            parts.append(f"{req['name']} (remote eligible)")
    return "; ".join(p for p in parts if p) or None


def _country(c: Any) -> str | None:
    if isinstance(c, dict):
        return c.get("name")
    return c


def _salary(posting: dict) -> tuple[int | None, int | None, str | None, str | None]:
    bs = posting.get("baseSalary")
    if not isinstance(bs, dict):
        return None, None, None, None
    val = bs.get("value")
    cur = bs.get("currency")
    if isinstance(val, dict):
        lo, hi = val.get("minValue"), val.get("maxValue")
        if lo is None and val.get("value") is not None:
            lo = hi = val.get("value")
        try:
            return (int(float(lo)) if lo is not None else None, int(float(hi)) if hi is not None else None,
                    cur, (val.get("unitText") or "").lower() or None)
        except (TypeError, ValueError):
            return None, None, cur, None
    return None, None, cur, None


def posting_to_job(posting: dict, page_url: str, company_name: str | None = None) -> JobData:
    org = posting.get("hiringOrganization")
    org_name = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else None)
    smin, smax, cur, period = _salary(posting)
    remote = "remote" if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE" else None
    ident = posting.get("identifier")
    ext = ident.get("value") if isinstance(ident, dict) else (str(ident) if ident else None)
    emp = posting.get("employmentType")
    return JobData(
        title=clean_ws(posting.get("title") or ""),
        company_name=company_name or org_name,
        location=_location(posting),
        remote_type=remote,
        description_text=html_to_text(posting.get("description") or ""),
        posted_at=parse_dt(posting.get("datePosted")),
        valid_through=parse_dt(posting.get("validThrough")),
        source_url=page_url,
        apply_url=posting.get("url") or page_url,
        external_id=str(ext) if ext else None,
        employment_type=", ".join(emp) if isinstance(emp, list) else emp,
        salary_min=smin, salary_max=smax, salary_currency=cur, salary_period=period,
        raw={"jsonld": True},
    )


def extract_from_html(html_text: str, page_url: str, company_name: str | None = None) -> tuple[list[JobData], list[str]]:
    """JSON-LD JobPostings from a page; falls back to basic title/text extraction."""
    soup = BeautifulSoup(html_text, "html.parser")
    jobs: list[JobData] = []
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for posting in _find_postings(data):
            job = posting_to_job(posting, page_url, company_name)
            if job.title:
                jobs.append(job)
    if jobs:
        return jobs, []
    # Fallback: page title and visible text. Clearly partial; user should review.
    title = None
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"]
    elif soup.find("h1"):
        title = soup.find("h1").get_text(" ", strip=True)
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = html_to_text(str(main))
    if not title:
        return [], ["No JobPosting data or title found on page"]
    return [JobData(title=clean_ws(title)[:300], company_name=company_name, description_text=text[:50000],
                    source_url=page_url, apply_url=page_url, raw={"jsonld": False, "partial": True})], \
        ["No structured JobPosting data; extracted page title and text only — please review the details"]


class JsonLdAdapter:
    kind = "jsonld"
    label = "Career pages (schema.org JobPosting)"
    config_help = '{"urls": ["https://careers.example.com/jobs/123", ...]}  — individual job pages (max 50)'

    def validate_config(self, config: dict) -> dict:
        urls = config.get("urls") or []
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split() if u.strip()]
        clean = []
        for u in urls:
            parts = urlsplit(u)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError(f"Invalid URL: {u}")
            clean.append(u)
        if not clean:
            raise ValueError("At least one URL is required")
        if len(clean) > MAX_PAGES_PER_SOURCE:
            raise ValueError(f"At most {MAX_PAGES_PER_SOURCE} URLs per source")
        return {"urls": clean}

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        result = FetchResult(method="html", full_snapshot=False)
        for url in self.validate_config(config)["urls"]:
            try:
                r = http.get(url, accept="text/html")
                jobs, warnings = extract_from_html(r.text, r.url or url, company_name)
                result.items.extend(jobs)
                result.errors.extend(f"{url}: {w}" for w in warnings)
            except Exception as exc:  # one bad page should not stop the rest
                result.errors.append(f"{url}: {exc}")
        return result


_GH_URL = re.compile(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_.-]+)/jobs/(\d+)")
_GH_EMBED = re.compile(r"[?&]for=([A-Za-z0-9_.-]+).*?[?&]token=(\d+)")
_LEVER_URL = re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)/([A-Za-z0-9-]{8,64})")


def fetch_url(http: PoliteHttp, url: str, company_name: str | None = None) -> tuple[JobData, list[str], str]:
    """Fetch a single job from a URL. Uses the ATS API when the URL is recognised.

    Returns (job, warnings, method).
    """
    m = _GH_URL.match(url)
    if m:
        job = GreenhouseAdapter().fetch_one(http, m.group(1), m.group(2), company_name)
        job.source_url = job.source_url or url
        return job, [], "api"
    m = _GH_EMBED.search(url)
    if m and "greenhouse" in url:
        job = GreenhouseAdapter().fetch_one(http, m.group(1), m.group(2), company_name)
        return job, [], "api"
    m = _LEVER_URL.match(url)
    if m:
        job = LeverAdapter().fetch_one(http, m.group(1), m.group(2), company_name)
        return job, [], "api"
    r = http.get(url, accept="text/html")
    jobs, warnings = extract_from_html(r.text, r.url or url, company_name)
    if not jobs:
        raise ValueError("; ".join(warnings) or "No job found at URL")
    job = jobs[0]
    if job.apply_url and not urlsplit(job.apply_url).netloc:
        job.apply_url = urljoin(url, job.apply_url)
    if len(jobs) > 1:
        warnings.append(f"Page contained {len(jobs)} postings; imported the first")
    return job, warnings, "html"


# ---- File imports --------------------------------------------------------

IMPORT_FIELDS = {
    "title": ("title", "job_title", "position", "role"),
    "company_name": ("company", "company_name", "employer", "organization"),
    "location": ("location", "city", "locations"),
    "remote_type": ("remote_type", "workplace", "workplace_type", "remote"),
    "description_text": ("description", "description_text", "job_description", "details"),
    "source_url": ("url", "source_url", "job_url", "link"),
    "apply_url": ("apply_url", "application_url", "apply_link"),
    "posted_at": ("posted_at", "date_posted", "posted", "date"),
    "external_id": ("external_id", "id", "job_id", "requisition_id"),
    "employment_type": ("employment_type", "type", "job_type"),
    "salary_min": ("salary_min", "min_salary"),
    "salary_max": ("salary_max", "max_salary"),
    "salary_currency": ("salary_currency", "currency"),
}
MAX_IMPORT_ROWS = 2000


def _row_to_job(row: dict) -> JobData | None:
    lower = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    values: dict[str, Any] = {}
    for field_name, aliases in IMPORT_FIELDS.items():
        for a in aliases:
            v = lower.get(a)
            if v not in (None, ""):
                values[field_name] = v.strip() if isinstance(v, str) else v
                break
    if not values.get("title"):
        return None
    for k in ("salary_min", "salary_max"):
        if k in values:
            try:
                values[k] = int(float(str(values[k]).replace(",", "")))
            except ValueError:
                values.pop(k)
    if "posted_at" in values:
        values["posted_at"] = parse_dt(values["posted_at"])
    if values.get("remote_type"):
        rt = str(values["remote_type"]).lower()
        values["remote_type"] = rt if rt in ("remote", "hybrid", "onsite") else None
    if values.get("external_id") is not None:
        values["external_id"] = str(values["external_id"])
    return JobData(raw={"import": True}, **values)


def parse_import(filename: str, content: bytes) -> tuple[list[JobData], list[str]]:
    text = content.decode("utf-8-sig", errors="replace")
    errors: list[str] = []
    rows: list[dict]
    if filename.lower().endswith(".json"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("jobs", [data])
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of job objects or {\"jobs\": [...]}")
        rows = [r for r in data if isinstance(r, dict)]
    elif filename.lower().endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        raise ValueError("Import supports .csv and .json files")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"Too many rows (max {MAX_IMPORT_ROWS})")
    jobs = []
    for i, row in enumerate(rows, start=1):
        job = _row_to_job(row)
        if job is None:
            errors.append(f"row {i}: missing title, skipped")
        else:
            jobs.append(job)
    return jobs, errors
