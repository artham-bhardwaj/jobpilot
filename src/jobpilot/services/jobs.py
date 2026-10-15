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
import re
from dataclasses import asdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import Company, Job, JobSighting, JobSource, SourceRun, utcnow
from jobpilot.services import audit, job_parser, settings_store
from jobpilot.services.text_utils import normalize_company, normalize_location, normalize_title, safe_url, sha256
from jobpilot.sources import ADAPTERS
from jobpilot.sources.base import JobData
from jobpilot.sources.http import FetchError, PoliteHttp, is_temporary

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


def structured_fields(data: JobData) -> dict[str, Any]:
    """Values the source supplied as structured data (as opposed to text we parsed)."""
    return {k: v for k, v in {
        "remote_type": data.remote_type, "employment_type": data.employment_type,
        "salary_min": data.salary_min, "salary_max": data.salary_max, "salary_currency": data.salary_currency,
        "valid_through": data.valid_through.isoformat() if data.valid_through else None,
        "location": data.location, "posted_at": data.posted_at.isoformat() if data.posted_at else None,
    }.items() if v not in (None, "")}


def _apply_data(job: Job, data: JobData) -> None:
    structured = structured_fields(data)
    parsed = job_parser.parse(data.title, data.description_text, data.location, structured=structured)
    job.title = data.title[:300]
    job.company_name = data.company_name
    job.location = data.location
    job.description_text = data.description_text
    job.employment_type = data.employment_type or parsed.employment_type
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
    job.raw = {**(data.raw or {}), "_structured": sorted(structured)}
    job.description_raw = data.description_raw or data.description_text
    job.apply_url_canonical = canonical_url(job.apply_url) or canonical_url(job.source_url)
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


TRACKING_PARAMS = re.compile(r"^(utm_.*|gh_src|ref|referrer|source|src|lever-source|lever-origin|trk|trkid|gclid|fbclid|"
                             r"mc_cid|mc_eid|_hsenc|_hsmi|igshid|li_fat_id|campaign)$", re.I)


def canonical_url(url: str | None) -> str | None:
    """Canonical form of a job/application URL for duplicate detection.

    Lower-cases scheme/host, drops default ports, fragments, tracking parameters and trailing
    ``/apply`` / ``/application`` segments, and sorts the remaining query parameters.
    """
    url = safe_url(url)
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.port and not ((parts.scheme == "http" and parts.port == 80) or (parts.scheme == "https" and parts.port == 443)):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/+", "/", parts.path or "/")
    path = re.sub(r"/(apply|application)/?$", "", path, flags=re.I).rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not TRACKING_PARAMS.match(k)))
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))[:1000]


def _canonicals(data: JobData) -> list[str]:
    return [c for c in dict.fromkeys((canonical_url(data.apply_url), canonical_url(data.source_url))) if c]


def find_existing(session: Session, source: JobSource | None, data: JobData) -> tuple[Job | None, str | None]:
    """Find a job this posting duplicates. Returns (job, matched_by)."""
    if source is not None and data.external_id:
        job = session.scalar(select(Job).where(Job.source_id == source.id, Job.external_id == data.external_id))
        if job:
            return job, "external_id"
    canon = _canonicals(data)
    if canon:
        job = session.scalar(select(Job).where(Job.apply_url_canonical.in_(canon)).order_by(Job.id.desc()))
        if job:
            return job, "application_url"
    fp = fingerprint(data.company_name, data.title, data.location)
    job = session.scalar(select(Job).where(Job.fingerprint == fp, Job.status == "active").order_by(Job.id.desc()))
    return (job, "fingerprint") if job else (None, None)


def upsert(session: Session, source: JobSource | None, data: JobData, method: str) -> tuple[Job, str]:
    """Insert or update one job.

    Outcomes: new | repost | duplicate (first seen via another source) | updated | reactivated | unchanged.
    """
    from jobpilot.services import skills_taxonomy

    skills_taxonomy.configure_from_settings(session)
    now = utcnow()
    data.source_url, data.apply_url = safe_url(data.source_url), safe_url(data.apply_url)
    existing, matched_by = find_existing(session, source, data)
    if existing is not None:
        outcome = "unchanged"
        new_hash = content_hash(data)
        if existing.status == "expired":
            existing.status = "active"
            existing.expired_at = None
            outcome = "reactivated"
        same_source = (source.id if source else None) == existing.source_id
        if same_source and existing.content_hash != new_hash:
            # Only the job's own source may change its content; other sources add provenance only.
            _apply_data(existing, data)
            existing.matched_at = None  # needs re-matching
            outcome = "updated" if outcome == "unchanged" else outcome
        first_from_this_source = source is not None and not any(s.source_id == source.id for s in existing.sightings)
        if outcome == "unchanged" and first_from_this_source and not same_source:
            outcome = "duplicate"
        existing.last_seen_at = now
        existing.last_checked_at, existing.last_check_status = now, "listed by source"
        if source is not None and existing.source_id == source.id:
            existing.miss_count = 0
        if existing.company_id is None:
            company = get_or_create_company(session, data.company_name)
            existing.company_id = company.id if company else None
        _add_sighting(session, existing, source, data, method)
        if outcome == "duplicate":
            audit.record(session, "job.duplicate_sighting", "job", existing.id, actor="system", matched_by=matched_by,
                         source_id=source.id if source else None)
        return existing, outcome

    company = get_or_create_company(session, data.company_name)
    job = Job(source_id=source.id if source else None, external_id=data.external_id,
              company_id=company.id if company else None, first_seen_at=now, last_seen_at=now, status="active",
              fingerprint="", title=data.title, last_checked_at=now, last_check_status="listed by source", miss_count=0)
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


OUTCOMES = ("new", "updated", "unchanged", "duplicate", "repost", "reactivated")


def ingest(session: Session, source: JobSource | None, items: list[JobData], method: str,
           full_snapshot: bool = False, gone_urls: list[str] | None = None) -> dict[str, Any]:
    """Store postings. Expiry is deliberately conservative:

    - A job is marked *missing* when a complete, successful snapshot of its own source omits it, and is
      **expired only after** ``expire_after_misses`` consecutive such snapshots.
    - An empty snapshot (or one where most active jobs vanish at once) is treated as a source **anomaly**:
      nothing is counted as missing or expired.
    - Failed, partial or malformed fetches never reach this point with ``full_snapshot=True``.
    """
    cfg = settings_store.get(session, "collection")
    stats: dict[str, Any] = {"fetched": len(items), **{o: 0 for o in OUTCOMES}, "expired": 0, "missing": 0,
                             "job_ids": [], "anomaly": None}
    seen_ids: set[int] = set()
    for data in items:
        if not data.title:
            continue
        job, outcome = upsert(session, source, data, method)
        stats[outcome] += 1
        seen_ids.add(job.id)
        if outcome in ("new", "updated", "repost", "reactivated"):
            stats["job_ids"].append(job.id)
    if source is not None and (full_snapshot or gone_urls):
        active = list(session.scalars(select(Job).where(Job.source_id == source.id, Job.status == "active")))
        if full_snapshot:
            absent = [j for j in active if j.id not in seen_ids]
            vanish_ratio = len(absent) / len(active) if active else 0.0
            if active and not seen_ids:
                stats["anomaly"] = f"source returned no jobs while {len(active)} were active — nothing expired"
            elif len(active) >= cfg["anomaly_min_active"] and vanish_ratio > cfg["anomaly_vanish_ratio"]:
                stats["anomaly"] = (f"{len(absent)} of {len(active)} active jobs vanished at once "
                                    f"({vanish_ratio:.0%}) — treated as a source problem, nothing expired")
            else:
                for job in absent:
                    _count_miss(session, job, cfg, stats, f"no longer listed by source '{source.name}'")
        gone = set(gone_urls or [])
        for job in active:
            if job.id not in seen_ids and job.source_url in gone and job.status == "active":
                _count_miss(session, job, cfg, stats, "configured page returned 404/410")
    stats["expired"] += expire_past_deadline(session)
    return stats


def _count_miss(session: Session, job: Job, cfg: dict, stats: dict, reason: str) -> None:
    job.miss_count = (job.miss_count or 0) + 1
    job.last_check_status = f"missing ({job.miss_count}/{cfg['expire_after_misses']})"
    if job.miss_count >= cfg["expire_after_misses"]:
        mark_expired(session, job, reason=f"{reason} in {job.miss_count} consecutive checks", actor="system")
        stats["expired"] += 1
    else:
        stats["missing"] += 1


def mark_expired(session: Session, job: Job, reason: str, actor: str = "user") -> None:
    if job.status != "expired":
        job.status = "expired"
        job.expired_at = utcnow()
        job.last_check_status = "expired"
        audit.record(session, "job.expired", "job", job.id, actor=actor, reason=reason)


def expire_past_deadline(session: Session) -> int:
    """Expire jobs whose *stated* closing date has passed: the source's structured ``validThrough``, or an
    explicit deadline sentence in the posting ("Applications close on 30 November 2026"). Nothing is fetched."""
    from datetime import date

    now = utcnow()
    today = now.date()
    n = 0
    for job in session.scalars(select(Job).where(Job.status == "active", Job.valid_through.is_not(None),
                                                 Job.valid_through < now)):
        mark_expired(session, job, reason="past its stated closing date (validThrough)", actor="system")
        n += 1
    for job in session.scalars(select(Job).where(Job.status == "active", Job.valid_through.is_(None),
                                                 Job.match_details.is_not(None))):
        ev = ((job.match_details or {}).get("requirements") or {}).get("evidence", {}).get("deadline") or {}
        if ev.get("basis") == "explicit" and ev.get("value"):
            try:
                deadline = date.fromisoformat(ev["value"])
            except ValueError:
                continue
            if deadline < today:
                mark_expired(session, job, reason=f"stated application deadline {deadline} passed (“{ev.get('snippet')}”)",
                             actor="system")
                n += 1
    return n


def check_alive(session: Session, http: PoliteHttp, job: Job, *, initiated_by_user: bool) -> str:
    """Re-check one job's posting URL **because you asked to**.

    URLs inside job data are untrusted, so this is never called automatically (scheduled expiry relies
    on source snapshots and stated closing dates instead). 404/410 → expired; 429/5xx/network errors →
    "temporarily unavailable" (no change).
    """
    if not initiated_by_user:
        raise PermissionError("Job URLs from job data are only fetched when you request a check")
    url = job.source_url or job.apply_url
    if not url:
        return "no URL to check"
    now = utcnow()
    try:
        http.get(url, accept="text/html")
    except FetchError as exc:
        job.last_checked_at = now
        if exc.status in (404, 410):
            mark_expired(session, job, reason=f"HTTP {exc.status} at {url}")
            return f"expired (HTTP {exc.status})"
        if is_temporary(exc):
            job.last_check_status = f"temporarily unavailable ({exc.status or 'network'})"
            return f"could not verify (temporarily unavailable: {exc})"
        job.last_check_status = f"check failed: {str(exc)[:40]}"
        return f"could not verify: {exc}"
    job.last_seen_at = job.last_checked_at = now
    job.last_check_status = "page reachable"
    audit.record(session, "job.checked", "job", job.id, result="reachable")
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


def run_source(session: Session, http: PoliteHttp, source: JobSource, *, dry_run: bool = False,
               trigger: str = "manual") -> dict[str, Any]:
    """Collect one source and record a ``SourceRun``.

    Status: ``success`` | ``partial`` (some pages/items failed; nothing expired) | ``unavailable`` (network,
    429 or 5xx: nothing changed) | ``failed`` (configuration/robots/not found) | ``anomaly`` (suspicious
    snapshot: nothing expired). With ``dry_run`` all job changes are rolled back; only the run record stays.
    """
    adapter = ADAPTERS[source.kind]
    before = dict(http.stats)
    run = SourceRun(source_id=source.id, dry_run=dry_run, trigger=trigger, status="running")
    session.add(run)
    session.flush()
    stats: dict[str, Any] = {"errors": [], "job_ids": []}
    try:
        result = adapter.fetch(http, source.config or {}, source.company_name)
    except Exception as exc:
        run.status = "unavailable" if is_temporary(exc) else "failed"
        run.error = str(exc)[:2000]
        stats["errors"] = [run.error]
        log.warning("source run failed", extra={"source": source.name, "status": run.status, "error": run.error[:300]})
    else:
        sp = session.begin_nested()
        try:
            stats.update(ingest(session, source, result.items, result.method,
                                full_snapshot=result.full_snapshot and not result.errors, gone_urls=result.gone_urls))
        except Exception as exc:  # a bug or bad data must not leave half-applied changes
            sp.rollback()
            log.exception("source ingest failed", extra={"source": source.name})
            run.status, run.error, run.finished_at = "failed", f"ingest error: {type(exc).__name__}: {exc}"[:2000], utcnow()
            source.last_run_at, source.last_status, source.last_error = run.started_at, "failed", run.error
            return {"status": "failed", "run_id": run.id, "errors": [run.error], "job_ids": [], "dry_run": dry_run}
        stats["errors"] = result.errors
        if dry_run:
            sp.rollback()
        else:
            sp.commit()
        if stats.get("anomaly"):
            run.status = "anomaly"
        elif result.errors and result.items:
            run.status = "partial"
        elif result.errors:  # nothing usable came back
            temporary_only = result.temporary_failures and result.temporary_failures >= len(result.errors)
            run.status = "unavailable" if temporary_only else "failed"
        else:
            run.status = "success"
        run.error = "\n".join(result.errors)[:2000] or None
        run.warnings = [stats["anomaly"]] if stats.get("anomaly") else None
        for key in ("fetched", "new", "updated", "unchanged", "expired", "missing"):
            setattr(run, key, stats.get(key, 0))
        run.duplicates, run.reposts = stats.get("duplicate", 0), stats.get("repost", 0)
    delta = {k: http.stats.get(k, 0) - before.get(k, 0) for k in http.stats}
    run.requests = sum(delta.values())
    run.rate_limited = delta.get("rate_limited", 0)
    run.http_errors = delta.get("client_error", 0) + delta.get("server_error", 0) + delta.get("network_error", 0)
    run.finished_at = utcnow()
    if not dry_run:
        source.last_run_at = run.started_at
        source.last_status = run.status
        source.last_error = run.error
        if run.status in ("success", "partial", "anomaly"):
            source.last_success_at = run.finished_at
        source.last_stats = {k: v for k, v in stats.items() if k not in ("job_ids",)}
        audit.record(session, "source.collected", "job_source", source.id, actor="system" if trigger != "manual" else "user",
                     status=run.status, new=run.new, expired=run.expired, rate_limited=run.rate_limited)
    stats["status"], stats["run_id"], stats["dry_run"] = run.status, run.id, dry_run
    return stats


def collect_all(session: Session, settings: Settings, http: PoliteHttp | None = None,
                source_ids: list[int] | None = None, *, dry_run: bool = False,
                trigger: str = "manual") -> dict[str, Any]:
    """Run all enabled sources (or the given ones), then score new/changed jobs (not in dry runs)."""
    from jobpilot.services import matching

    http = http or PoliteHttp(settings)
    q = select(JobSource).where(JobSource.kind.in_(list(ADAPTERS)))
    q = q.where(JobSource.id.in_(source_ids)) if source_ids else q.where(JobSource.enabled.is_(True))
    summary: dict[str, Any] = {"sources": {}, "new": 0, "failed": 0, "dry_run": dry_run}
    for source in session.scalars(q):
        stats = run_source(session, http, source, dry_run=dry_run, trigger=trigger)
        summary["sources"][source.name] = {k: v for k, v in stats.items() if k != "job_ids"}
        summary["new"] += stats.get("new", 0) + stats.get("repost", 0)
        if stats["status"] in ("failed", "unavailable"):
            summary["failed"] += 1
    summary["matched"] = 0 if dry_run else matching.match_pending(session)
    summary["requests"] = http.request_count
    prefix = "DRY RUN (nothing saved): " if dry_run else ""
    summary["message"] = f"{prefix}{summary['new']} new job(s); {summary['failed']} source(s) failed or unavailable"
    return summary


def source_health(session: Session) -> list[dict[str, Any]]:
    """Per-source health summary for the Sources page."""
    from sqlalchemy import func

    from jobpilot.services import scheduler

    sched = settings_store.get(session, "scheduler")["tasks"].get("collect_jobs", {})
    next_run = None
    if sched.get("enabled"):
        last = scheduler.last_success(session, "collect_jobs")
        next_run = (last + timedelta(hours=float(sched.get("every_hours", 24)))) if last else utcnow()
    rows = []
    for src in session.scalars(select(JobSource).order_by(JobSource.name)):
        runs = list(session.scalars(select(SourceRun).where(SourceRun.source_id == src.id, SourceRun.dry_run.is_(False))
                                    .order_by(SourceRun.id.desc()).limit(10)))
        sighted = select(JobSighting.job_id).where(JobSighting.source_id == src.id)
        rows.append({
            "source": src, "runs": runs, "last_run": runs[0] if runs else None,
            "jobs_total": session.scalar(select(func.count(func.distinct(JobSighting.job_id))).where(JobSighting.source_id == src.id)) or 0,
            "jobs_active": session.scalar(select(func.count(Job.id)).where(Job.id.in_(sighted), Job.status == "active")) or 0,
            "rate_limited_recent": sum(r.rate_limited for r in runs),
            "errors_recent": sum(1 for r in runs if r.status in ("failed", "unavailable", "partial", "anomaly")),
            "next_run": next_run if src.enabled and src.kind in ADAPTERS else None,
            "dry_runs": list(session.scalars(select(SourceRun).where(SourceRun.source_id == src.id, SourceRun.dry_run.is_(True))
                                             .order_by(SourceRun.id.desc()).limit(3))),
        })
    return rows


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
