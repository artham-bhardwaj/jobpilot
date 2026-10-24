"""Career-page adapter (schema.org JobPosting JSON-LD), manual URLs and file imports."""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from jobpilot.services.text_utils import clean_ws, description_to_text, html_to_text
from jobpilot.sources.ats import GreenhouseAdapter, LeverAdapter
from jobpilot.sources.base import FetchResult, JobData, parse_deadline, parse_dt
from jobpilot.sources.http import PoliteHttp, is_temporary

log = logging.getLogger(__name__)

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
            local = [addr.get("addressLocality"), addr.get("addressRegion")]
            country = _country(addr.get("addressCountry"))
            # Some sites already put the country in the locality ("Bengaluru, India"): don't repeat it.
            if country and any(country.lower() in str(b or "").lower() for b in local):
                country = None
            parts.append(", ".join(str(b) for b in local + [country] if b))
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
    deadline, _ = parse_deadline(posting.get("validThrough"), parse_dt(posting.get("datePosted")))
    return JobData(
        title=clean_ws(posting.get("title") or ""),
        company_name=company_name or org_name,
        location=_location(posting),
        remote_type=remote,
        description_text=description_to_text(posting.get("description") or ""),
        description_raw=posting.get("description") or None,
        posted_at=parse_dt(posting.get("datePosted")),
        valid_through=deadline,
        source_url=page_url,
        apply_url=posting.get("url") or page_url,
        external_id=str(ext) if ext else None,
        employment_type=", ".join(emp) if isinstance(emp, list) else emp,
        salary_min=smin, salary_max=smax, salary_currency=cur, salary_period=period,
        raw={"jsonld": True, **({"deadline_source": "schema.org validThrough"} if deadline else {})},
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
                    description_raw=str(main)[:200000],
                    source_url=page_url, apply_url=page_url, raw={"jsonld": False, "partial": True})], \
        ["No structured JobPosting data; extracted page title and text only — please review the details"]


_WORKDAY_JOB = re.compile(r"^https://(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                          r"(?P<site>[A-Za-z0-9_-]+)/(?P<rest>job/.+)$")


def enrich_workday(http: PoliteHttp, job: JobData) -> JobData:
    """Workday's JobPosting markup flattens the description into one line (headings, bullets and line breaks lost),
    so requirements can't be told from preferences. The same site's job API (robots.txt allows it; it is what the
    posting page itself loads) has the description as HTML: use that. Any failure keeps the JobPosting text."""
    m = _WORKDAY_JOB.match((job.source_url or "").split("?")[0])
    if not m or (job.description_text and "\n" in job.description_text):
        return job
    api = (f"https://{m['tenant']}.{m['wd']}.myworkdayjobs.com/wday/cxs/{m['tenant']}/{m['site']}/{m['rest']}")
    try:
        info = (http.get_json(api) or {}).get("jobPostingInfo") or {}
    except Exception as exc:  # the JobPosting data is still usable
        log.info("workday description unavailable", extra={"url": api, "error": str(exc)[:200]})
        return job
    html_desc = info.get("jobDescription")
    if not isinstance(html_desc, str) or not html_desc.strip():
        return job
    text = description_to_text(html_desc)
    if len(text) < 0.5 * len(job.description_text or ""):  # implausibly short: keep what we had
        return job
    job.raw = {**(job.raw or {}), "description_source": "workday job API (HTML)",
               "jsonld_description_chars": len(job.description_text or "")}
    job.description_raw, job.description_text = html_desc, text
    return job


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
                result.items.extend(enrich_workday(http, j) for j in jobs)
                result.errors.extend(f"{url}: {w}" for w in warnings)
            except Exception as exc:  # one bad page should not stop the rest
                if getattr(exc, "status", None) in (404, 410):
                    result.gone_urls.append(url)
                elif is_temporary(exc):
                    result.temporary_failures += 1
                result.errors.append(f"{url}: {exc}")
        return result


class SitemapAdapter:
    """Job pages listed in a career site's own sitemap (the one its robots.txt points to).

    Reads the sitemap, keeps posting URLs whose URL text matches your title keywords (and optional location
    keywords), then reads at most ``max_pages`` of those pages for their schema.org JobPosting data, exactly like
    the career-page source. Works for any site that publishes a sitemap and JobPosting markup, e.g. Workday career
    sites (``https://<tenant>.wd<N>.myworkdayjobs.com/<site>/siteMap.xml``).

    - Only URLs on the sitemap's own host are fetched; robots.txt and per-host rate limits apply to every request.
    - Sitemaps may list only part of the openings (Workday's list about 100), so jobs missing from a run are never
      expired from it; a posting page that answers 404/410 is.
    """

    kind = "sitemap"
    label = "Career-site sitemap (JobPosting pages)"
    config_help = ('{"sitemap_url": "https://<tenant>.wd5.myworkdayjobs.com/<site>/siteMap.xml", '
                   '"title_keywords": ["software engineer", "backend"], "location_keywords": ["india"], '
                   '"exclude_keywords": ["director"], "max_pages": 25}')
    MAX_SITEMAP_URLS = 5000

    def validate_config(self, config: dict) -> dict:
        url = str(config.get("sitemap_url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or not parts.path.lower().endswith(".xml"):
            raise ValueError("sitemap_url must be an https URL of an .xml sitemap")

        def words(key: str, required: bool = False) -> list[str]:
            v = config.get(key) or []
            if isinstance(v, str):
                v = [x for x in re.split(r"[,\n]", v)]
            out = [_slug_text(str(x)) for x in v if _slug_text(str(x))]
            if required and not out:
                raise ValueError(f"{key}: at least one keyword is required (only matching postings are read)")
            return out

        try:
            max_pages = int(config.get("max_pages") or 25)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_pages must be a number") from exc
        if not 1 <= max_pages <= MAX_PAGES_PER_SOURCE:
            raise ValueError(f"max_pages must be between 1 and {MAX_PAGES_PER_SOURCE}")
        return {"sitemap_url": url, "title_keywords": words("title_keywords", True),
                "location_keywords": words("location_keywords"), "exclude_keywords": words("exclude_keywords"),
                "max_pages": max_pages}

    def select(self, config: dict, locs: list[str]) -> tuple[list[str], int]:
        """(posting URLs to read, how many matched before the page cap)."""
        host = urlsplit(config["sitemap_url"]).netloc.lower()
        matched = []
        for loc in dict.fromkeys(locs):  # keeps order, drops repeats
            parts = urlsplit(loc)
            if parts.scheme != "https" or parts.netloc.lower() != host:
                continue
            text = _slug_text(parts.path)
            if not any(_has_phrase(text, k) for k in config["title_keywords"]):
                continue
            if config["location_keywords"] and not any(_has_phrase(text, k) for k in config["location_keywords"]):
                continue
            if any(_has_phrase(text, k) for k in config["exclude_keywords"]):
                continue
            matched.append(loc)
        return matched[:config["max_pages"]], len(matched)

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult:
        cfg = self.validate_config(config)
        r = http.get(cfg["sitemap_url"], accept="application/xml, text/xml")
        locs = [html.unescape(m) for m in _LOC.findall(r.text)][:self.MAX_SITEMAP_URLS]
        if not locs:
            return FetchResult(method="sitemap", errors=["The sitemap contains no URLs (or is not a sitemap)"])
        urls, matched = self.select(cfg, locs)
        result = JsonLdAdapter().fetch(http, {"urls": urls}, company_name) if urls else FetchResult(method="html")
        result.method, result.full_snapshot = "sitemap", False
        result.counts = {"sitemap_urls": len(locs), "matched": matched, "read": len(urls)}
        result.notes.append(f"sitemap lists {len(locs)} posting(s); {matched} matched your keywords; read {len(urls)}"
                            + (f" (max_pages {cfg['max_pages']})" if matched > len(urls) else ""))
        return result


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def _slug_text(s: str) -> str:
    return clean_ws(re.sub(r"[^a-z0-9+#]+", " ", s.lower()))


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


_GH_URL = re.compile(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_.-]+)/jobs/(\d+)")
_GH_EMBED = re.compile(r"[?&]for=([A-Za-z0-9_.-]+).*?[?&]token=(\d+)")
_GH_BOARD = re.compile(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/[A-Za-z0-9_.-]+/?(?:\?.*)?$")
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
    if _GH_BOARD.match(url):
        raise ValueError("That is a Greenhouse board's list of openings, not a job posting: paste one job's URL, "
                         "or add the board as a Greenhouse source")
    r = http.get(url, accept="text/html")
    final = r.url or url
    if _GH_BOARD.match(final) or ("greenhouse.io" in final and "error=true" in final):
        # Greenhouse redirects a closed or unknown posting to the board index with ?error=true.
        raise ValueError("Greenhouse no longer shows that posting (it redirected to the board's list of openings)")
    jobs, warnings = extract_from_html(r.text, final, company_name)
    if not jobs:
        raise ValueError("; ".join(warnings) or "No job found at URL")
    job = enrich_workday(http, jobs[0])
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
    return JobData(raw={"import": True}, description_raw=values.get("description_text"), **values)


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
