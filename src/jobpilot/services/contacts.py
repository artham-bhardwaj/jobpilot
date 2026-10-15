"""Contacts with provenance, verification status, dedup and do-not-contact handling.

- Guessed addresses are stored as ``guessed`` and never shown or used as verified.
- ``verified`` requires an explicit note describing how it was verified.
- Do-not-contact entries (email, domain or company) block drafting and sending.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from jobpilot.models import Company, Contact, ContactInteraction, DoNotContact, Job, utcnow
from jobpilot.services import audit
from jobpilot.services.jobs import get_or_create_company
from jobpilot.services.profile import EMAIL_PATTERN
from jobpilot.services.text_utils import normalize

BASE_CONFIDENCE = {"job_posting": 0.8, "company_website": 0.75, "email_thread": 0.9, "referral": 0.7,
                   "public_profile": 0.6, "manual": 0.5}


class ContactIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    name: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    job_id: int | None = None
    role_type: Literal["recruiter", "hiring_manager", "referral", "engineer", "other"] = "recruiter"
    email: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    email_status: Literal["verified", "unverified", "guessed"] | None = None
    linkedin_url: HttpUrl | None = None
    phone: str | None = Field(default=None, max_length=50)
    source_type: Literal["job_posting", "company_website", "public_profile", "email_thread", "referral", "manual"] = "manual"
    source_url: HttpUrl | None = None
    source_note: str | None = Field(default=None, max_length=2000)
    relationship_note: str | None = Field(default=None, max_length=2000)
    verification_note: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str | None) -> str | None:
        return v.lower() if v else v

    @field_validator("email_status")
    @classmethod
    def _status_needs_email(cls, v, info):
        if v and not info.data.get("email"):
            return None
        return v


class DncIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    kind: Literal["email", "domain", "company"]
    value: str = Field(min_length=2, max_length=320)
    reason: str | None = Field(default=None, max_length=1000)


def _confidence(c: Contact) -> float:
    base = BASE_CONFIDENCE.get(c.source_type, 0.5)
    if c.email_status == "verified":
        return 1.0
    if c.email_status == "guessed":
        return min(base, 0.2)
    if c.source_url:
        base = min(1.0, base + 0.05)
    return round(base, 2)


def find_duplicate(session: Session, data: ContactIn, company_id: int | None, exclude_id: int | None = None) -> Contact | None:
    conds = []
    if data.email:
        conds.append(func.lower(Contact.email) == data.email.lower())
    if data.linkedin_url:
        conds.append(Contact.linkedin_url == str(data.linkedin_url))
    for c in session.scalars(select(Contact).where(or_(*conds))) if conds else []:
        if c.id != exclude_id:
            return c
    if data.name and company_id:
        n = normalize(data.name)
        for c in session.scalars(select(Contact).where(Contact.company_id == company_id)):
            if c.id != exclude_id and c.name and normalize(c.name) == n:
                return c
    return None


def save_contact(session: Session, data: ContactIn, contact_id: int | None = None) -> tuple[Contact, bool]:
    """Create/update a contact. Returns (contact, merged_into_existing)."""
    if not (data.name or data.email or data.linkedin_url):
        raise ValueError("A contact needs at least a name, an email or a profile URL")
    if data.email_status == "verified" and not data.verification_note:
        raise ValueError("Explain how the email was verified (e.g. 'they replied from this address')")
    if data.role_type == "referral" and not data.relationship_note:
        raise ValueError("For referral contacts, describe how you actually know this person")
    company = get_or_create_company(session, data.company_name)
    company_id = company.id if company else None
    if company_id is None and data.job_id:
        job = session.get(Job, data.job_id)
        company_id = job.company_id if job else None

    merged = False
    if contact_id is None:
        existing = find_duplicate(session, data, company_id)
        if existing is not None:
            contact, merged = existing, True
        else:
            contact = Contact(discovered_at=utcnow())
            session.add(contact)
    else:
        contact = session.get(Contact, contact_id)
        if contact is None:
            raise LookupError("Contact not found")

    values = data.model_dump(exclude={"company_name"})
    for k in ("linkedin_url", "source_url"):
        values[k] = str(values[k]) if values[k] else None
    if values.get("email") and not values.get("email_status"):
        values["email_status"] = "unverified"
    for k, v in values.items():
        if merged and (v is None or getattr(contact, k) not in (None, "")):
            continue  # merging never overwrites existing values
        setattr(contact, k, v)
    if company_id and (not merged or contact.company_id is None):
        contact.company_id = company_id
    if contact.email_status == "verified" and contact.verified_at is None:
        contact.verified_at = utcnow()
    if contact.email_status != "verified":
        contact.verified_at = None
    contact.confidence = _confidence(contact)
    if contact.email and is_blocked(session, contact.email, contact.company_id):
        contact.do_not_contact = True
        contact.dnc_reason = contact.dnc_reason or "matches do-not-contact list"
    session.flush()
    audit.record(session, "contact.merged" if merged else ("contact.updated" if contact_id else "contact.created"),
                 "contact", contact.id, source_type=contact.source_type, email_status=contact.email_status)
    return contact, merged


def contacts_from_job(session: Session, job: Job) -> list[Contact]:
    """Create contacts for e-mail addresses published in the job posting itself."""
    emails = ((job.match_details or {}).get("requirements") or {}).get("emails") or []
    created = []
    for email in emails:
        local = email.split("@")[0].lower()
        generic = local in ("jobs", "careers", "recruiting", "talent", "hr", "hiring", "recruitment", "apply") or \
            any(w in local for w in ("recruit", "talent", "career", "jobs"))
        c, _ = save_contact(session, ContactIn(
            email=email, company_name=job.company_name, job_id=job.id,
            role_type="recruiter", source_type="job_posting",
            source_url=job.source_url or job.apply_url or None,
            source_note=f"address published in job posting #{job.id}" + (" (team inbox)" if generic else ""),
            name="Recruiting team" if generic else None))
        created.append(c)
    return created


def verify_email(session: Session, contact: Contact, note: str) -> None:
    if not contact.email:
        raise ValueError("Contact has no email")
    if not note.strip():
        raise ValueError("A verification note is required")
    contact.email_status = "verified"
    contact.verified_at = utcnow()
    contact.verification_note = note.strip()
    contact.confidence = _confidence(contact)
    audit.record(session, "contact.email_verified", "contact", contact.id)


def set_dnc(session: Session, contact: Contact, flag: bool, reason: str | None = None) -> None:
    contact.do_not_contact = flag
    contact.dnc_reason = reason if flag else None
    if flag and contact.email:
        add_dnc(session, DncIn(kind="email", value=contact.email, reason=reason or f"contact #{contact.id}"))
    if not flag and contact.email:
        for row in session.scalars(select(DoNotContact).where(DoNotContact.kind == "email",
                                                              DoNotContact.value == contact.email.lower())):
            session.delete(row)
    audit.record(session, "contact.dnc.set" if flag else "contact.dnc.cleared", "contact", contact.id)
    _suppress_followups_for(session, contact)


def _suppress_followups_for(session: Session, contact: Contact) -> None:
    from jobpilot.models import EmailDraft

    if not contact.do_not_contact:
        return
    for d in session.scalars(select(EmailDraft).where(EmailDraft.contact_id == contact.id,
                                                      EmailDraft.follow_up_due_at.is_not(None))):
        d.follow_up_due_at = None
        d.suppressed_reason = "contact marked do-not-contact"


def add_dnc(session: Session, data: DncIn) -> DoNotContact:
    value = data.value.lower().lstrip("@") if data.kind in ("email", "domain") else normalize(data.value)
    existing = session.scalar(select(DoNotContact).where(DoNotContact.kind == data.kind, DoNotContact.value == value))
    if existing:
        return existing
    row = DoNotContact(kind=data.kind, value=value, reason=data.reason)
    session.add(row)
    session.flush()
    # Flag matching contacts.
    for c in session.scalars(select(Contact)):
        if c.email and is_blocked(session, c.email, c.company_id) and not c.do_not_contact:
            c.do_not_contact = True
            c.dnc_reason = f"do-not-contact {data.kind}: {value}"
            _suppress_followups_for(session, c)
    audit.record(session, "dnc.added", "do_not_contact", row.id, kind=data.kind)
    return row


def remove_dnc(session: Session, dnc_id: int) -> None:
    row = session.get(DoNotContact, dnc_id)
    if row:
        session.delete(row)
        audit.record(session, "dnc.removed", "do_not_contact", dnc_id)


def is_blocked(session: Session, email: str | None, company_id: int | None = None) -> str | None:
    """Return the reason an address/company is on the do-not-contact list, else None."""
    if email:
        e = email.lower()
        domain = e.split("@")[-1]
        row = session.scalar(select(DoNotContact).where(
            or_((DoNotContact.kind == "email") & (DoNotContact.value == e),
                (DoNotContact.kind == "domain") & (DoNotContact.value == domain))))
        if row:
            return f"{row.kind} {row.value} is on the do-not-contact list"
    if company_id:
        company = session.get(Company, company_id)
        if company:
            row = session.scalar(select(DoNotContact).where(DoNotContact.kind == "company",
                                                            DoNotContact.value == normalize(company.name)))
            if row:
                return f"company {company.name} is on the do-not-contact list"
    return None


def log_interaction(session: Session, contact: Contact, channel: str, direction: str, summary: str,
                    draft_id: int | None = None) -> ContactInteraction:
    if channel not in ("email", "linkedin", "phone", "in_person", "other") or direction not in ("outbound", "inbound"):
        raise ValueError("Invalid channel or direction")
    it = ContactInteraction(contact_id=contact.id, channel=channel, direction=direction, summary=summary[:5000],
                            draft_id=draft_id)
    session.add(it)
    audit.record(session, "contact.interaction", "contact", contact.id, channel=channel, direction=direction)
    return it


def search(session: Session, q: str | None = None, role_type: str | None = None, company_id: int | None = None,
           include_dnc: bool = True) -> list[Contact]:
    stmt = select(Contact).order_by(Contact.updated_at.desc())
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.outerjoin(Company, Contact.company_id == Company.id).where(or_(
            func.lower(Contact.name).like(like), func.lower(Contact.email).like(like),
            func.lower(Contact.title).like(like), func.lower(Company.name).like(like)))
    if role_type:
        stmt = stmt.where(Contact.role_type == role_type)
    if company_id:
        stmt = stmt.where(Contact.company_id == company_id)
    if not include_dnc:
        stmt = stmt.where(Contact.do_not_contact.is_(False))
    return list(session.scalars(stmt.limit(500)))
