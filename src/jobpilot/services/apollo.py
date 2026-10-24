"""Recruiter contacts from Apollo — manual import, or the optional API (off by default).

Manual mode (no API, no key): export contacts you found in Apollo's own interface as CSV and import them
(``jobpilot recruiters import FILE``), or add one (``jobpilot recruiters add``). Each becomes a contact with source
``apollo_manual`` and an email status that says only what Apollo reported (``apollo_reported``), never "verified".

API mode (``apollo.enabled`` setting **and** ``JOBPILOT_APOLLO_API_KEY``), documented endpoints only (2026-10-18):

- People API Search ``POST /api/v1/mixed_people/api_search`` — **0 credits**; returns names (last name obfuscated),
  titles and an ``has_email`` flag, never emails. Used to find recruiter candidates at a company domain.
- People Enrichment ``POST /api/v1/people/match`` — returns the work email (``person.email``) and Apollo's
  ``email_status``; **about 1 credit when an email is returned**. Called for one person at a time, only with an
  explicit ``spend_credit=True`` and within ``apollo.max_enrichments_per_day`` (default 0). Personal emails and phone
  numbers are never requested (``reveal_personal_emails`` / ``reveal_phone_number`` false), and a personal-domain
  address that comes back anyway is discarded.

No LinkedIn data is collected by JobPilot: LinkedIn profile URLs are only what you paste in.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import AuditLog, Contact, Job, utcnow
from jobpilot.services import audit, settings_store
from jobpilot.services import contacts as contact_svc

log = logging.getLogger(__name__)

API = "https://api.apollo.io/api/v1"
TIMEOUT = 30


class ApolloError(RuntimeError):
    pass


# Apollo's own words for an email's state → what JobPilot records (it never upgrades a third-party claim).
_STATUS = {"verified": "apollo_reported", "valid": "apollo_reported", "likely to engage": "unverified",
           "unverified": "unverified", "extrapolated": "guessed", "guessed": "guessed", "unavailable": None,
           "bounced": "bounced", "invalid": "bounced"}


def map_status(apollo_status: str | None) -> str | None:
    return _STATUS.get((apollo_status or "").strip().lower(), "unverified")


def _clean_email(email: str | None) -> tuple[str | None, str | None]:
    """(work email or None, why it was dropped)."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return None, None
    if contact_svc.email_domain(e) in contact_svc.PERSONAL_DOMAINS:
        return None, f"{contact_svc.email_domain(e)} is a personal address; not stored"
    return e, None


# ---- manual mode --------------------------------------------------------------------------------------------

_COLUMNS = {"first": ("first name", "first_name"), "last": ("last name", "last_name"), "name": ("name", "full name"),
            "title": ("title", "job title"), "company": ("company", "company name", "company name for emails",
                                                     "organization"),
            "email": ("email", "work email", "email address"), "status": ("email status", "email_status"),
            "linkedin": ("person linkedin url", "linkedin url", "linkedin")}


def _col(row: dict[str, str], key: str) -> str:
    for name in _COLUMNS[key]:
        for k, v in row.items():
            if (k or "").strip().lower() == name and (v or "").strip():
                return v.strip()
    return ""


def import_csv(session: Session, content: bytes, job_id: int | None = None) -> dict[str, Any]:
    """Contacts from an Apollo CSV export (manual mode). Never fetches anything."""
    text = content.decode("utf-8-sig", errors="replace")
    out = {"created": 0, "merged": 0, "skipped": 0, "warnings": []}
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        name = _col(row, "name") or " ".join(x for x in (_col(row, "first"), _col(row, "last")) if x)
        email, dropped = _clean_email(_col(row, "email"))
        linkedin = _col(row, "linkedin") or None
        if linkedin and not linkedin.startswith(("http://", "https://")):
            linkedin = "https://" + linkedin
        if not (name or email or linkedin):
            out["skipped"] += 1
            continue
        status = map_status(_col(row, "status")) if email else None
        try:
            contact, merged = contact_svc.save_contact(session, contact_svc.ContactIn(
                name=name or None, title=_col(row, "title") or None, company_name=_col(row, "company") or None,
                job_id=job_id, role_type="recruiter", email=email, email_status=status, linkedin_url=linkedin,
                source_type="apollo_manual", source_note=f"Apollo CSV import, row {i}"))
        except ValueError as exc:
            out["skipped"] += 1
            out["warnings"].append(f"row {i}: {exc}"[:200])
            continue
        out["merged" if merged else "created"] += 1
        for warn in [dropped, contact_svc.domain_warning(contact)]:
            if warn:
                out["warnings"].append(f"row {i} ({contact.display_name}): {warn}")
    audit.record(session, "apollo.csv_imported", "contact", None, created=out["created"], merged=out["merged"],
                 skipped=out["skipped"])
    return out


# ---- API mode (optional) --------------------------------------------------------------------------------------

def api_status(session: Session, settings: Settings) -> dict[str, Any]:
    cfg = settings_store.get(session, "apollo")
    problems = []
    if not cfg["enabled"]:
        problems.append("Apollo API mode is off (Settings → apollo: enabled)")
    if settings.apollo_api_key is None:
        problems.append("JOBPILOT_APOLLO_API_KEY is not set")
    return {"ready": not problems, "problems": problems, "enrichments_today": enrichments_today(session),
            "max_enrichments_per_day": cfg["max_enrichments_per_day"]}


def enrichments_today(session: Session) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "apollo.enrichment",
                                                                 AuditLog.ts >= start)) or 0


class ApolloClient:
    def __init__(self, settings: Settings, http: requests.Session | None = None):
        if settings.apollo_api_key is None:
            raise ApolloError("JOBPILOT_APOLLO_API_KEY is not set")
        self._key = settings.apollo_api_key
        self.http = http or requests.Session()

    def __repr__(self) -> str:  # never show the key
        return "ApolloClient(key=****)"

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = self.http.post(f"{API}{path}", json=payload, timeout=TIMEOUT,
                               headers={"x-api-key": self._key.get_secret_value(), "Content-Type": "application/json",
                                        "Cache-Control": "no-cache"})
        except requests.RequestException as exc:
            raise ApolloError(f"Could not reach Apollo ({type(exc).__name__})") from exc
        if r.status_code in (401, 403):
            raise ApolloError("Apollo rejected the API key or this plan does not allow the endpoint")
        if r.status_code == 429:
            raise ApolloError("Apollo rate limit reached; try again later (not retried automatically)")
        if r.status_code >= 400:
            raise ApolloError(f"Apollo answered HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise ApolloError("Apollo returned an unreadable response") from exc

    def search_people(self, domain: str, titles: list[str], country: str | None, per_page: int = 10) -> dict:
        payload: dict[str, Any] = {"q_organization_domains_list": [domain], "person_titles": titles,
                                   "page": 1, "per_page": max(1, min(per_page, 25))}
        if country:
            payload["person_locations"] = [country]
        return self._post("/mixed_people/api_search", payload)

    def enrich(self, apollo_id: str) -> dict:
        return self._post("/people/match", {"id": apollo_id, "reveal_personal_emails": False,
                                            "reveal_phone_number": False})


def _require_api(session: Session, settings: Settings) -> dict:
    st = api_status(session, settings)
    if not st["ready"]:
        raise ApolloError("; ".join(st["problems"]))
    return settings_store.get(session, "apollo")


def search_recruiters(session: Session, settings: Settings, domain: str, client: ApolloClient | None = None,
                      per_page: int = 10) -> list[dict[str, Any]]:
    """Recruiter candidates at ``domain`` (0 credits). Nothing is stored: a search result is not a verified contact."""
    cfg = _require_api(session, settings)
    domain = (domain or "").strip().lower().removeprefix("www.")
    if "." not in domain or "/" in domain:
        raise ApolloError("Give the employer's web domain, e.g. rubrik.com")
    client = client or ApolloClient(settings)
    data = client.search_people(domain, cfg["recruiter_titles"], cfg.get("country"), per_page)
    people = data.get("people") if isinstance(data, dict) else None
    if not isinstance(people, list):
        raise ApolloError("Unexpected Apollo search response")
    audit.record(session, "apollo.search", "company", None, domain=domain, results=len(people), credits=0)
    return [{"apollo_id": p.get("id"), "name": " ".join(x for x in (p.get("first_name"), p.get("last_name_obfuscated")) if x),
             "title": p.get("title"), "company": (p.get("organization") or {}).get("name"),
             "has_email": bool(p.get("has_email"))} for p in people if p.get("id")]


def enrich_contact(session: Session, settings: Settings, apollo_id: str, *, job: Job | None = None,
                   spend_credit: bool = False, client: ApolloClient | None = None) -> tuple[Contact | None, str]:
    """One person's work email via People Enrichment. Uses a credit only with ``spend_credit`` and within the daily
    cap. Returns (contact or None, what happened). Never fabricates: no email returned means none is stored."""
    cfg = _require_api(session, settings)
    if not spend_credit:
        raise ApolloError("Enrichment can use an Apollo credit: repeat with --spend-credit to confirm")
    if enrichments_today(session) >= int(cfg["max_enrichments_per_day"]):
        raise ApolloError(f"Daily enrichment limit reached ({cfg['max_enrichments_per_day']}); "
                          "raise apollo.max_enrichments_per_day to allow more")
    client = client or ApolloClient(settings)
    data = client.enrich(apollo_id)
    audit.record(session, "apollo.enrichment", "contact", None, apollo_id=str(apollo_id)[:64],
                 email_returned=bool((data.get("person") or {}).get("email")))
    person = data.get("person") if isinstance(data, dict) else None
    if not person:
        return None, "Apollo found no matching person (no credit is charged for that)"
    email, dropped = _clean_email(person.get("email"))
    org = person.get("organization") or {}
    name = person.get("name") or " ".join(x for x in (person.get("first_name"), person.get("last_name")) if x)
    linkedin = person.get("linkedin_url") or None
    contact, merged = contact_svc.save_contact(session, contact_svc.ContactIn(
        name=name or None, title=person.get("title") or None, company_name=org.get("name") or (job.company_name if job else None),
        job_id=job.id if job else None, role_type="recruiter", email=email,
        email_status=map_status(person.get("email_status")) if email else None, linkedin_url=linkedin,
        source_type="apollo_api", source_note=f"Apollo People Enrichment, id {str(apollo_id)[:64]}"))
    notes = [("merged into an existing contact" if merged else "contact created"),
             (f"work email {email} ({contact.email_status})" if email else "no work email returned")]
    for warn in (dropped, contact_svc.domain_warning(contact)):
        if warn:
            notes.append(warn)
    return contact, "; ".join(notes)
