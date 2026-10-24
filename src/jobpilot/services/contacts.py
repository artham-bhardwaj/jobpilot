"""Contacts with provenance, verification status, dedup and do-not-contact handling.

- Guessed addresses are stored as ``guessed`` and never shown or used as verified.
- ``verified`` requires an explicit note describing how it was verified.
- Do-not-contact entries (email, domain or company) block drafting and sending.
"""

from __future__ import annotations

import re

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
                   "public_profile": 0.6, "manual": 0.5, "linkedin_manual": 0.6, "apollo_manual": 0.6, "apollo_api": 0.6}
SOURCE_TYPES = ("job_posting", "company_website", "public_profile", "email_thread", "referral", "manual",
                "linkedin_manual", "apollo_manual", "apollo_api")
# verified: you confirmed it (they replied from it / it's on the company site); apollo_reported: Apollo says its check
# passed, JobPilot did not confirm it; unverified / guessed: as named; bounced: mail to it came back.
EMAIL_STATUSES = ("verified", "apollo_reported", "unverified", "guessed", "bounced")
PERSONAL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
                    "icloud.com", "me.com", "proton.me", "protonmail.com", "rediffmail.com", "aol.com", "gmx.com",
                    "zoho.com", "yandex.com"}


def email_domain(email: str | None) -> str:
    return (email or "").rsplit("@", 1)[-1].lower() if email and "@" in email else ""


def domain_warning(contact: Contact) -> str | None:
    """A visible warning when the email's domain doesn't fit the contact's company (never changed silently)."""
    dom = email_domain(contact.email)
    if not dom:
        return None
    if dom in PERSONAL_DOMAINS:
        return f"{dom} is a personal email domain, not a work address"
    company = (contact.company.name if contact.company else "") or ""
    tokens = [t for t in re.split(r"[^a-z0-9]+", company.lower()) if len(t) >= 3 and t not in
              {"inc", "ltd", "llc", "corp", "corporation", "limited", "private", "pvt", "the", "technologies", "services",
               "solutions", "group", "company", "india", "global"}]
    if tokens and not any(t in dom.replace("-", "") for t in tokens):
        return f"email domain {dom} does not mention {company!r}; check it belongs to this company"
    return None


class ContactIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    name: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    job_id: int | None = None
    role_type: Literal["recruiter", "hiring_manager", "referral", "engineer", "other"] = "recruiter"
    email: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    email_status: Literal["verified", "apollo_reported", "unverified", "guessed", "bounced"] | None = None
    linkedin_url: HttpUrl | None = None
    phone: str | None = Field(default=None, max_length=50)
    source_type: Literal["job_posting", "company_website", "public_profile", "email_thread", "referral", "manual",
                         "linkedin_manual", "apollo_manual", "apollo_api"] = "manual"
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
    if c.email_status == "bounced":
        return 0.0
    if c.email_status == "guessed":
        return min(base, 0.2)
    if c.email_status == "apollo_reported":
        base = min(1.0, base + 0.15)  # a third-party check passed; still not confirmed by you
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
    if data.job_id:
        job = session.get(Job, data.job_id)
        if job:
            link_contact_job(session, contact, job, data.role_type,
                             "address published in the posting" if data.source_type == "job_posting" else "entered for this job")
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
        if session.scalar(select(Contact.id).where(func.lower(Contact.email) == e, Contact.email_status == "bounced")):
            return f"mail to {e} bounced earlier"
    if company_id:
        company = session.get(Company, company_id)
        if company:
            row = session.scalar(select(DoNotContact).where(DoNotContact.kind == "company",
                                                            DoNotContact.value == normalize(company.name)))
            if row:
                return f"company {company.name} is on the do-not-contact list"
    return None


def record_bounce(session: Session, contact: Contact, evidence: str) -> None:
    """Mail to this address came back: it is marked bounced (blocked for all outreach) and pending follow-ups stop."""
    if contact.email_status == "bounced":
        return
    contact.email_status, contact.verified_at = "bounced", None
    contact.confidence = _confidence(contact)
    contact.verification_note = ((contact.verification_note or "") + f"\nBounced: {evidence}").strip()[:2000]
    _suppress_followups_for(session, contact)
    audit.record(session, "contact.email_bounced", "contact", contact.id)


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


# ---- Job associations, duplicates and safe merging ------------------------------------------

def link_contact_job(session: Session, contact: Contact, job: Job, relation: str | None = None,
                     source: str = "linked by you") -> "ContactJob":
    """Associate a contact with a job (idempotent). The job's application inherits the association."""
    from jobpilot.models import ContactJob

    existing = session.scalar(select(ContactJob).where(ContactJob.contact_id == contact.id, ContactJob.job_id == job.id))
    if existing:
        return existing
    link = ContactJob(contact_id=contact.id, job_id=job.id, relation=relation or contact.role_type, source=source[:200])
    session.add(link)
    session.flush()
    audit.record(session, "contact.linked_job", "contact", contact.id, job_id=job.id, source=source[:80])
    return link


def delete_contact(session: Session, contact: Contact, reason: str) -> dict:
    """Remove a contact that should never have existed (e.g. a test placeholder).

    Refused when any email draft or logged interaction refers to it — those keep their history; correct the contact
    instead. The audit entry records what was removed and why, so the history stays readable after the row is gone.
    """
    from jobpilot.models import ContactJob, EmailDraft

    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Say why the contact is being removed (e.g. 'test placeholder')")
    drafts = session.scalar(select(func.count(EmailDraft.id)).where(EmailDraft.contact_id == contact.id)) or 0
    talks = session.scalar(select(func.count(ContactInteraction.id)).where(ContactInteraction.contact_id == contact.id)) or 0
    if drafts or talks:
        raise ValueError(f"{contact.display_name} has {drafts} email draft(s) and {talks} interaction(s); keep it "
                         "(edit it, or mark it do-not-contact) so that history stays linked")
    jobs = [link.job_id for link in session.scalars(select(ContactJob).where(ContactJob.contact_id == contact.id))]
    snapshot = {"name": contact.name, "title": contact.title, "company": contact.company.name if contact.company else None,
                "email": contact.email, "linkedin_url": contact.linkedin_url, "source_type": contact.source_type,
                "jobs": jobs}
    for link in session.scalars(select(ContactJob).where(ContactJob.contact_id == contact.id)):
        session.delete(link)
    cid = contact.id
    session.delete(contact)
    session.flush()
    audit.record(session, "contact.deleted", "contact", cid, reason=reason[:300], removed=snapshot)
    return {"removed": cid, **snapshot}


def unlink_contact_job(session: Session, contact: Contact, job: Job) -> None:
    from jobpilot.models import ContactJob

    for link in session.scalars(select(ContactJob).where(ContactJob.contact_id == contact.id, ContactJob.job_id == job.id)):
        session.delete(link)
        audit.record(session, "contact.unlinked_job", "contact", contact.id, job_id=job.id)


def jobs_for_contact(session: Session, contact: Contact) -> list[tuple[Job, str, str | None]]:
    """(job, relation, how-established) for every job associated with the contact."""
    from jobpilot.models import ContactJob

    rows = [(link.job, link.relation, link.source) for link in
            session.scalars(select(ContactJob).where(ContactJob.contact_id == contact.id).order_by(ContactJob.id))]
    if contact.job and all(j.id != contact.job_id for j, _, _ in rows):
        rows.insert(0, (contact.job, contact.role_type, "found on this job"))
    return rows


MERGE_FIELDS = ("name", "title", "company_id", "job_id", "role_type", "email", "email_status", "linkedin_url", "phone",
                "source_type", "source_url", "source_note", "relationship_note", "verification_note", "notes")


def duplicate_candidates(session: Session) -> list[dict]:
    """Pairs of contacts that may be the same person, with the reasons. Nothing is merged automatically."""
    contacts = list(session.scalars(select(Contact).order_by(Contact.id)))
    out, seen = [], set()
    for i, a in enumerate(contacts):
        for b in contacts[i + 1:]:
            reasons = []
            if a.name and b.name and normalize(a.name) == normalize(b.name):
                if a.company_id and a.company_id == b.company_id:
                    reasons.append("same name at the same company")
                elif not a.company_id or not b.company_id:
                    reasons.append("same name (one has no company)")
            if a.email and b.email and a.email.lower() == b.email.lower():
                reasons.append("same email")
            if a.linkedin_url and b.linkedin_url and a.linkedin_url.rstrip("/").lower() == b.linkedin_url.rstrip("/").lower():
                reasons.append("same profile URL")
            if reasons and (a.id, b.id) not in seen:
                seen.add((a.id, b.id))
                out.append({"a": a, "b": b, "reasons": reasons, "conflicts": merge_conflicts(a, b)})
    return out


def merge_conflicts(keep: Contact, other: Contact) -> list[tuple[str, object, object]]:
    return [(f, getattr(keep, f), getattr(other, f)) for f in MERGE_FIELDS
            if getattr(keep, f) not in (None, "") and getattr(other, f) not in (None, "")
            and getattr(keep, f) != getattr(other, f)]


def merge_contacts(session: Session, keep: Contact, other: Contact) -> dict:
    """Merge ``other`` into ``keep``. Safe rules:

    - ``keep``'s values always win; ``other`` only fills empty fields; conflicts are reported, not resolved.
    - Do-not-contact is never lost: if either is DNC, the result is DNC.
    - A *verified* email is kept verified only if that email is the one that survives.
    - History, drafts and job associations move to ``keep``; ``other`` is deleted. Audited.
    """
    from jobpilot.models import ContactInteraction, ContactJob, EmailDraft

    if keep.id == other.id:
        raise ValueError("Choose two different contacts")
    conflicts = merge_conflicts(keep, other)
    filled = []
    for f in MERGE_FIELDS:
        if getattr(keep, f) in (None, "") and getattr(other, f) not in (None, ""):
            setattr(keep, f, getattr(other, f))
            filled.append(f)
    if "email" in filled:
        keep.verified_at = other.verified_at
    if keep.email_status == "verified" and not keep.verification_note:
        keep.email_status = "unverified"
    if other.do_not_contact and not keep.do_not_contact:
        keep.do_not_contact, keep.dnc_reason = True, other.dnc_reason or f"merged from contact #{other.id}"
    keep.discovered_at = min(keep.discovered_at, other.discovered_at)
    keep.notes = "\n".join(x for x in (keep.notes, f"[merged contact #{other.id}]") if x)
    for it in session.scalars(select(ContactInteraction).where(ContactInteraction.contact_id == other.id)):
        it.contact_id = keep.id
    for d in session.scalars(select(EmailDraft).where(EmailDraft.contact_id == other.id)):
        d.contact_id = keep.id
    keep_jobs = set(session.scalars(select(ContactJob.job_id).where(ContactJob.contact_id == keep.id)))
    for link in session.scalars(select(ContactJob).where(ContactJob.contact_id == other.id)):
        if link.job_id in keep_jobs:
            session.delete(link)
        else:
            link.contact_id = keep.id
    keep.confidence = _confidence(keep)
    session.flush()
    session.delete(other)
    session.flush()
    audit.record(session, "contact.merged_records", "contact", keep.id, merged_id=other.id, filled=filled,
                 conflicts=[c[0] for c in conflicts])
    return {"filled": filled, "conflicts": conflicts}
