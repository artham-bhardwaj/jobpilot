"""Job ingestion: dedup, provenance, repost/expiry detection, source runs.

Dedup rules (in order):
1. Same source + external id                → same job.
2. Same apply URL                           → same job.
3. Same fingerprint (company+title+location) and the job is active
                                            → same job seen elsewhere (new sighting).
4. Same fingerprint but the old job expired → a **repost**: new job linked via ``repost_of_id``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import Company, Job, JobSighting, JobSource, utcnow
from jobpilot.services import audit, job_parser
from jobpilot.services.text_utils import normalize_company, normalize_location, normalize_title, safe_url, sha256
from jobpilot.sources import ADAPTERS
from jobpilot.sources.base import JobData
from jobpilot.sources.http import FetchError, PoliteHttp

log = logging.getLogger(__name__)

MANUAL_SOURCE = "Manual entry"
IMPORT_SOURCE = "File import"


def fingerprint(company: str | None, title: str, location: str | None) -> str:
    return sha256(f"{normalize_company(company)}|{normalize_title(title)}|{normalize_location(location)}")[:40]


def content_hash(job: JobData) -> str:
    return sha256("|".join(str(x or "") for x in (job.title, job.location, job.description_text,
                                                    job.salary_min, job.salary_max)))


def get_or_create_company(session: Session, name: str | None) -> Company | None:
    if not name or not name.strip():
        return None
    norm = normalize_company(name) or name.strip().lower()
    company = session.scalar(select(Company).where(Company.normalized_name == norm))
    if company is None:
        company = Company(name=name.strip(), normalized_name=norm)
        session.add(company)
        session.flush()
    return company


def get_or_create_source(session: Session, name: str, kind: str) -> JobSource:
    src = session.scalar(select(JobSource).where(JobSource.name == name))
    if src is None:
        src = JobSource(name=name, kind=kind, enabled=True, config={})
        session.add(src)
        session.flush()
    return src


def _apply_data(job: Job, data: JobData) -> None:
    parsed = job_parser.parse(data.title, data.description_text, data.location)
    job.title = data.title[:300]
    job.company_name = data.company_name
    job.location = data.location
    job.description_text = data.description_text
    job.employment_type = data.employment_type
    job.department = data.department
    job.posted_at = data.posted_at or job.posted_at
    job.valid_through = data.valid_through
    job.source_url = data.source_url or job.source_url
    job.apply_url = data.apply_url or job.apply_url
    job.remote_type = data.remote_type or parsed.remote_type
    job.experience_min_years = parsed.experience_min_years
    job.experience_max_years = parsed.experience_max_years
    job.experience_level = parsed.experience_level
    job.salary_min = data.salary_min if data.salary_min is not None else parsed.salary_min
    job.salary_max = data.salary_max if data.salary_max is not None else parsed.salary_max
    job.salary_currency = data.salary_currency or (parsed.salary_currency if parsed.salary_min else None)
    job.salary_period = data.salary_period
    job.raw = data.raw
    job.content_hash = content_hash(data)
    job.fingerprint = fingerprint(data.company_name, data.title, data.location)


def _add_sighting(session: Session, job: Job, source: JobSource | None, data: JobData, method: str) -> None:
    now = utcnow()
    for s in job.sightings:
        if s.source_id == (source.id if source else None) and s.external_id == data.external_id \
                and s.url == data.source_url:
            s.seen_at = now
            s.times_seen += 1
            return
    job.sightings.append(JobSighting(source_id=source.id if source else None, external_id=data.external_id,
                                     url=data.source_url, method=method, first_seen_at=now, seen_at=now))


def find_existing(session: Session, source: JobSource | None, data: JobData) -> Job | None:
    if source is not None and data.external_id:
        job = session.scalar(select(Job).where(Job.source_id == source.id, Job.external_id == data.external_id))
        if job:
            return job
    if data.apply_url:
        job = session.scalar(select(Job).where(Job.apply_url == data.apply_url).order_by(Job.id.desc()))
        if job:
            return job
    fp = fingerprint(data.company_name, data.title, data.location)
    return session.scalar(select(Job).where(Job.fingerprint == fp, Job.status == "active").order_by(Job.id.desc()))


def upsert(session: Session, source: JobSource | None, data: JobData, method: str) -> tuple[Job, str]:
    """Insert or update one job. Returns (job, outcome) with outcome in new|updated|unchanged|repost|reactivated."""
    now = utcnow()
    data.source_url, data.apply_url = safe_url(data.source_url), safe_url(data.apply_url)
    existing = find_existing(session, source, data)
    if existing is not None:
        outcome = "unchanged"
        new_hash = content_hash(data)
        if existing.status == "expired":
            existing.status = "active"
            existing.expired_at = None
            outcome = "reactivated"
        if existing.content_hash != new_hash:
            _apply_data(existing, data)
            existing.matched_at = None  # needs re-matching
            outcome = "updated" if outcome == "unchanged" else outcome
        existing.last_seen_at = now
        if existing.company_id is None:
            company = get_or_create_company(session, data.company_name)
            existing.company_id = company.id if company else None
        _add_sighting(session, existing, source, data, method)
        return existing, outcome

    company = get_or_create_company(session, data.company_name)
    job = Job(source_id=source.id if source else None, external_id=data.external_id,
              company_id=company.id if company else None, first_seen_at=now, last_seen_at=now, status="active",
              fingerprint="", title=data.title)
    _apply_data(job, data)
    outcome = "new"
    prior = session.scalar(select(Job).where(Job.fingerprint == job.fingerprint, Job.status == "expired")
                           .order_by(Job.id.desc()))
    if prior is not None:
        job.repost_of_id = prior.id
        outcome = "repost"
    session.add(job)
    _add_sighting(session, job, source, data, method)
    session.flush()
    return job, outcome


def ingest(session: Session, source: JobSource | None, items: list[JobData], method: str,
           full_snapshot: bool = False) -> dict[str, Any]:
    stats = {"fetched": len(items), "new": 0, "updated": 0, "unchanged": 0, "repost": 0, "reactivated": 0,
             "expired": 0, "job_ids": []}
    seen_ids: set[int] = set()
    for data in items:
        if not data.title:
            continue
        job, outcome = upsert(session, source, data, method)
        stats[outcome] += 1
        seen_ids.add(job.id)
        if outcome in ("new", "updated", "repost", "reactivated"):
            stats["job_ids"].append(job.id)
    if full_snapshot and source is not None:
        stale = session.scalars(select(Job).where(Job.source_id == source.id, Job.status == "active",
                                                  Job.id.not_in(seen_ids) if seen_ids else Job.id.is_not(None)))
        for job in stale:
            mark_expired(session, job, reason=f"no longer listed by source '{source.name}'", actor="system")
            stats["expired"] += 1
    stats["expired"] += expire_past_deadline(session)
    return stats


def mark_expired(session: Session, job: Job, reason: str, actor: str = "user") -> None:
    if job.status != "expired":
        job.status = "expired"
        job.expired_at = utcnow()
        audit.record(session, "job.expired", "job", job.id, actor=actor, reason=reason)


def expire_past_deadline(session: Session) -> int:
    now = utcnow()
    n = 0
    for job in session.scalars(select(Job).where(Job.status == "active", Job.valid_through.is_not(None),
                                                 Job.valid_through < now)):
        mark_expired(session, job, reason="past validThrough date", actor="system")
        n += 1
    return n


def check_alive(session: Session, http: PoliteHttp, job: Job) -> str:
    """Re-check a job URL; marks it expired on 404/410. Returns a status message."""
    url = job.source_url or job.apply_url
    if not url:
        return "no URL to check"
    try:
        http.get(url, accept="text/html")
    except FetchError as exc:
        if exc.status in (404, 410):
            mark_expired(session, job, reason=f"HTTP {exc.status} at {url}")
            return f"expired (HTTP {exc.status})"
        return f"could not verify: {exc}"
    job.last_seen_at = utcnow()
    return "still reachable"


# ---- Sources -------------------------------------------------------------

class SourceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    kind: str
    company_name: str | None = Field(default=None, max_length=200)
    config: dict[str, Any] = {}
    enabled: bool = True
    terms_note: str | None = Field(default=None, max_length=2000)


def save_source(session: Session, data: SourceIn, source_id: int | None = None) -> JobSource:
    adapter = ADAPTERS.get(data.kind)
    if adapter is None:
        raise ValueError(f"Unknown source kind {data.kind!r}")
    config = adapter.validate_config(data.config)
    clash = session.scalar(select(JobSource).where(JobSource.name == data.name))
    if clash is not None and clash.id != source_id:
        raise ValueError("A source with this name already exists")
    if source_id is None:
        src = JobSource(name=data.name, kind=data.kind)
        session.add(src)
    else:
        src = session.get(JobSource, source_id)
        if src is None:
            raise LookupError("Source not found")
    src.name, src.kind, src.company_name, src.config = data.name, data.kind, data.company_name, config
    src.enabled, src.terms_note = data.enabled, data.terms_note
    session.flush()
    audit.record(session, "source.saved", "job_source", src.id, kind=src.kind)
    return src


def run_source(session: Session, http: PoliteHttp, source: JobSource) -> dict[str, Any]:
    adapter = ADAPTERS[source.kind]
    source.last_run_at = utcnow()
    try:
        result = adapter.fetch(http, source.config or {}, source.company_name)
    except Exception as exc:
        source.last_status = "failed"
        source.last_error = str(exc)[:2000]
        log.warning("source failed", extra={"source": source.name, "error": str(exc)})
        raise
    stats = ingest(session, source, result.items, result.method,
                   full_snapshot=result.full_snapshot and not result.errors)
    stats["errors"] = result.errors
    source.last_status = "partial" if result.errors else "success"
    source.last_error = "\n".join(result.errors)[:2000] or None
    source.last_stats = {k: v for k, v in stats.items() if k != "job_ids"}
    audit.record(session, "source.collected", "job_source", source.id, actor="system",
                 **{k: v for k, v in stats.items() if k not in ("job_ids", "errors")})
    return stats


def collect_all(session: Session, settings: Settings, http: PoliteHttp | None = None,
                source_ids: list[int] | None = None) -> dict[str, Any]:
    """Run all enabled sources (or the given ones), then match new/changed jobs."""
    from jobpilot.services import matching

    http = http or PoliteHttp(settings)
    q = select(JobSource).where(JobSource.kind.in_(list(ADAPTERS)))
    q = q.where(JobSource.id.in_(source_ids)) if source_ids else q.where(JobSource.enabled.is_(True))
    summary: dict[str, Any] = {"sources": {}, "new": 0, "failed": 0}
    for source in session.scalars(q):
        try:
            with session.begin_nested():
                stats = run_source(session, http, source)
            summary["sources"][source.name] = {k: v for k, v in stats.items() if k != "job_ids"}
            summary["new"] += stats["new"] + stats["repost"]
        except Exception as exc:
            summary["failed"] += 1
            summary["sources"][source.name] = {"error": str(exc)}
            source.last_status, source.last_error, source.last_run_at = "failed", str(exc)[:2000], utcnow()
    summary["matched"] = matching.match_pending(session)
    summary["requests"] = http.request_count
    summary["message"] = f"{summary['new']} new job(s); {summary['failed']} source(s) failed"
    return summary


# ---- Manual entry --------------------------------------------------------

class ManualJobIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=300)
    company_name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=300)
    remote_type: str | None = Field(default=None, pattern=r"^(remote|hybrid|onsite)$")
    description_text: str | None = Field(default=None, max_length=100_000)
    source_url: HttpUrl | None = None
    apply_url: HttpUrl | None = None
    posted_at: datetime | None = None
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=10)


def add_manual(session: Session, data: ManualJobIn) -> tuple[Job, str]:
    from jobpilot.services import matching

    values = data.model_dump()
    for k in ("source_url", "apply_url"):
        values[k] = str(values[k]) if values[k] else None
    source = get_or_create_source(session, MANUAL_SOURCE, "manual")
    job, outcome = upsert(session, source, JobData(raw={"manual": True}, **values), "manual")
    matching.match_job(session, job)
    audit.record(session, f"job.manual.{outcome}", "job", job.id)
    return job, outcome


def add_from_url(session: Session, http: PoliteHttp, url: str, company_name: str | None = None) -> tuple[Job, str, list[str]]:
    from jobpilot.services import matching
    from jobpilot.sources.pages import fetch_url

    data, warnings, method = fetch_url(http, url, company_name)
    if not data.title:
        raise ValueError("Could not find a job title at that URL")
    source = get_or_create_source(session, MANUAL_SOURCE, "manual")
    job, outcome = upsert(session, source, data, method)
    if warnings:
        job.notes = ((job.notes or "") + "\nImport warnings: " + "; ".join(warnings)).strip()
    matching.match_job(session, job)
    audit.record(session, f"job.url.{outcome}", "job", job.id, url=url)
    return job, outcome, warnings


def import_file(session: Session, filename: str, content: bytes) -> dict[str, Any]:
    from jobpilot.services import matching
    from jobpilot.sources.pages import parse_import

    items, errors = parse_import(filename, content)
    source = get_or_create_source(session, IMPORT_SOURCE, "import")
    stats = ingest(session, source, items, "import", full_snapshot=False)
    for jid in stats["job_ids"]:
        matching.match_job(session, session.get(Job, jid))
    stats["errors"] = errors
    audit.record(session, "job.imported", "job_source", source.id, file=filename,
                 new=stats["new"], updated=stats["updated"])
    return stats


# ---- Querying ------------------------------------------------------------

SORTS = {
    "relevance": (Job.relevance_score.desc().nulls_last(), Job.id.desc()),
    "newest": (Job.first_seen_at.desc(),),
    "posted": (Job.posted_at.desc().nulls_last(),),
    "company": (Job.company_name.asc(),),
    "title": (Job.title.asc(),),
}


def search(session: Session, *, q: str | None = None, status: str | None = "active", category: str | None = None,
           eligibility: str | None = None, remote: str | None = None, user_status: str | None = None,
           min_score: float | None = None, source_id: int | None = None, posted_within_days: int | None = None,
           sort: str = "relevance", limit: int = 100, offset: int = 0) -> tuple[list[Job], int]:
    from sqlalchemy import func

    stmt = select(Job)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Job.title).like(like), func.lower(Job.company_name).like(like),
                              func.lower(Job.location).like(like), func.lower(Job.description_text).like(like)))
    if status:
        stmt = stmt.where(Job.status == status)
    if category:
        stmt = stmt.where(Job.category == category)
    if eligibility:
        stmt = stmt.where(Job.eligibility == eligibility)
    if remote:
        stmt = stmt.where(Job.remote_type == remote)
    if user_status:
        stmt = stmt.where(Job.user_status == user_status)
    else:
        stmt = stmt.where(Job.user_status != "dismissed")
    if min_score is not None:
        stmt = stmt.where(Job.relevance_score >= min_score)
    if source_id:
        stmt = stmt.where(Job.sightings.any(JobSighting.source_id == source_id))
    if posted_within_days:
        cutoff = utcnow() - timedelta(days=posted_within_days)
        stmt = stmt.where(or_(Job.posted_at >= cutoff, (Job.posted_at.is_(None)) & (Job.first_seen_at >= cutoff)))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(*SORTS.get(sort, SORTS["relevance"])).limit(limit).offset(offset)
    return list(session.scalars(stmt)), total


def set_user_status(session: Session, job: Job, status: str) -> None:
    if status not in ("new", "shortlisted", "dismissed"):
        raise ValueError("Invalid status")
    job.user_status = status
    audit.record(session, f"job.{status}", "job", job.id)


def job_data_dict(data: JobData) -> dict:
    return asdict(data)
