"""Common types for job-source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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
