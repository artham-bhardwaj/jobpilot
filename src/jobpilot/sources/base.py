"""Common types for job-source adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from jobpilot.sources.http import PoliteHttp


@dataclass
class JobData:
    """Normalised job posting produced by every adapter."""

    title: str
    company_name: str | None = None
    location: str | None = None
    remote_type: str | None = None  # remote|hybrid|onsite (None = infer)
    description_text: str | None = None
    posted_at: datetime | None = None
    valid_through: datetime | None = None
    source_url: str | None = None
    apply_url: str | None = None
    external_id: str | None = None
    employment_type: str | None = None
    department: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    raw: dict[str, Any] | None = None
    # The description exactly as received (HTML or text), preserved for provenance.
    description_raw: str | None = None


@dataclass
class FetchResult:
    items: list[JobData] = field(default_factory=list)
    # True when ``items`` is the complete current list for the source, so jobs
    # missing from it can be marked expired.
    full_snapshot: bool = False
    errors: list[str] = field(default_factory=list)
    method: str = "api"
    # Configured URLs that answered 404/410 (definitive "gone"), for page-based sources.
    gone_urls: list[str] = field(default_factory=list)
    # How many of ``errors`` were temporary (network, 429, 5xx) rather than definitive.
    temporary_failures: int = 0
    # Informational facts about the run (e.g. how many sitemap postings matched); not errors.
    notes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


class SourceAdapter(Protocol):
    kind: str
    label: str
    config_help: str

    def validate_config(self, config: dict) -> dict: ...

    def fetch(self, http: PoliteHttp, config: dict, company_name: str | None) -> FetchResult: ...


def parse_dt(value: Any) -> datetime | None:
    """Parse ISO strings or epoch milliseconds into naive UTC datetimes."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)
        s = str(value).strip().replace("Z", "+00:00")
        if len(s) == 10:
            return datetime.fromisoformat(s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt
    except (ValueError, OSError, OverflowError):
        return None


_ISO_DEADLINE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def parse_deadline(value: Any, posted_at: datetime | None = None) -> tuple[datetime | None, str | None]:
    """Strictly parse a source-supplied application deadline. Returns ``(deadline, problem)``.

    Only ISO dates (``2026-11-30``) and ISO date-times are accepted; anything else is ignored, never guessed.
    A date without a time means the end of that day. Implausible values (outside 2000-2100, or more than a day
    before the posting date) are ignored too, so a bad value can never expire a job.
    """
    if value in (None, ""):
        return None, None
    s = str(value).strip()
    if not isinstance(value, str) or not _ISO_DEADLINE.match(s):
        return None, f"not an ISO date: {s[:40]!r}"
    try:
        if len(s) == 10:
            dt = datetime.fromisoformat(s).replace(hour=23, minute=59, second=59)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        return None, f"invalid date: {s[:40]!r}"
    if not 2000 <= dt.year <= 2100:
        return None, f"implausible year: {s[:40]!r}"
    if posted_at is not None and dt < posted_at - timedelta(days=1):
        return None, f"before the posting date: {s[:40]!r}"
    return dt, None
