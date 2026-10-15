"""Deterministic email drafts from templates and verified facts.

- Rendering uses a sandboxed Jinja2 environment.
- Every value comes from stored records; only **confirmed** candidate facts are
  used. A value that is unknown renders as ``[[MISSING: name]]``; drafts with
  such placeholders cannot be approved until you fill them in yourself.
- Templates make no claims about relationships, referrals or past
  conversations. Referral requests require a referral contact whose
  relationship you have described.
- Nothing is sent automatically. Sending happens manually (copy) or through
  the optional Gmail integration after explicit approval.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Contact, EmailDraft, EmailTemplate, Job, ProfileLink, ResumeVersion, utcnow
from jobpilot.services import audit, contacts as contact_svc, profile as profile_svc, settings_store
from jobpilot.services.profile import EMAIL_PATTERN

MISSING_RE = re.compile(r"\[\[MISSING: ([^\]]+)\]\]")
OUTREACH_KINDS = ("recruiter_outreach", "hiring_manager", "referral_request", "status_inquiry")
KIND_LABELS = {
    "recruiter_outreach": "Recruiter outreach",
    "referral_request": "Referral request",
    "hiring_manager": "Hiring manager outreach",
    "follow_up": "Follow-up",
    "thank_you": "Thank-you (after interview)",
    "status_inquiry": "Application status inquiry",
}

_SIGNATURE = "{{ candidate.full_name }}{% if candidate.email %}\n{{ candidate.email }}{% endif %}" \
             "{% if candidate.phone %}\n{{ candidate.phone }}{% endif %}{% for l in candidate.links %}\n{{ l }}{% endfor %}"

BUILTIN_TEMPLATES: dict[str, dict[str, str]] = {
    "recruiter_outreach": {
        "name": "Recruiter outreach",
        "subject": "{{ job.title }} role at {{ company.name }}",
        "body": (
            "Hi {{ contact.first_name or 'there' }},\n\n"
            "I'm {{ candidate.full_name }}{% if candidate.headline %}, {{ candidate.headline }}{% endif %}. "
            "I'm interested in the {{ job.title }} position at {{ company.name }}"
            "{% if job.url %} ({{ job.url }}){% endif %}.\n\n"
            "{% if candidate.matching_skills %}My experience includes {{ candidate.matching_skills }}, which the posting "
            "lists among its requirements.\n\n{% endif %}"
            "{% if application.applied_on %}I applied on {{ application.applied_on }}. {% endif %}"
            "{% if resume.label %}My resume is attached. {% endif %}"
            "Would you be open to a short conversation about whether my background fits the role?\n\n"
            "Thank you for your time,\n" + _SIGNATURE),
    },
    "hiring_manager": {
        "name": "Hiring manager outreach",
        "subject": "Interest in the {{ job.title }} role",
        "body": (
            "Hi {{ contact.first_name or 'there' }},\n\n"
            "I came across the {{ job.title }} opening on your team at {{ company.name }}"
            "{% if job.url %} ({{ job.url }}){% endif %}. "
            "{% if candidate.matching_skills %}I have worked with {{ candidate.matching_skills }}, "
            "and I'd value the chance to contribute.{% else %}I'd value the chance to contribute.{% endif %}\n\n"
            "{% if application.applied_on %}I applied on {{ application.applied_on }}. {% endif %}"
            "If you are open to it, I'd appreciate a few minutes to learn more about what the team needs.\n\n"
            "Best regards,\n" + _SIGNATURE),
    },
    "referral_request": {
        "name": "Referral request",
        "subject": "Question about the {{ job.title }} role at {{ company.name }}",
        "body": (
            "Hi {{ contact.first_name }},\n\n"
            "I hope you're doing well. I noticed {{ company.name }} is hiring for {{ job.title }}"
            "{% if job.url %} ({{ job.url }}){% endif %}"
            "{% if candidate.matching_skills %}, and my experience with {{ candidate.matching_skills }} seems relevant{% endif %}.\n\n"
            "Would you be comfortable referring me for this role, or pointing me to the right person? "
            "I'm happy to share my resume and anything else that would help. "
            "No problem at all if it isn't possible.\n\n"
            "Thanks,\n" + _SIGNATURE),
    },
    "follow_up": {
        "name": "Follow-up",
        "subject": "Re: {{ previous.subject }}",
        "body": (
            "Hi {{ contact.first_name or 'there' }},\n\n"
            "I wanted to follow up on my message from {{ previous.sent_on }} about the {{ job.title }} role at "
            "{{ company.name }}. I'm still very interested and would be glad to share any further information.\n\n"
            "Thank you,\n" + _SIGNATURE),
    },
    "thank_you": {
        "name": "Thank-you after interview",
        "subject": "Thank you — {{ job.title }} interview",
        "body": (
            "Hi {{ contact.first_name or 'there' }},\n\n"
            "Thank you for taking the time to speak with me on {{ interview.date }} about the {{ job.title }} role "
            "at {{ company.name }}. I enjoyed learning more about the team and remain very interested in the position.\n\n"
            "Please let me know if there is anything else I can provide.\n\n"
            "Best regards,\n" + _SIGNATURE),
    },
    "status_inquiry": {
        "name": "Application status inquiry",
        "subject": "Application status — {{ job.title }}",
        "body": (
            "Hi {{ contact.first_name or 'there' }},\n\n"
            "I applied for the {{ job.title }} position at {{ company.name }} on {{ application.applied_on }} "
            "and wanted to ask whether there is any update on my application. I remain very interested in the role.\n\n"
            "Thank you for your time,\n" + _SIGNATURE),
    },
}


class Missing:
    """Falsy placeholder for an unknown value; renders as a visible marker."""

    def __init__(self, name: str, sink: set[str]):
        self.name, self.sink = name, sink

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        self.sink.add(self.name)
        return f"[[MISSING: {self.name}]]"

    __html__ = __str__


class _Undef(ChainableUndefined):
    def __str__(self) -> str:
        return f"[[MISSING: {self._undefined_name}]]"


_env = SandboxedEnvironment(undefined=_Undef, autoescape=False, keep_trailing_newline=False)


def render(template: str, context: dict[str, Any]) -> tuple[str, list[str]]:
    text = _env.from_string(template).render(**context)
    return text, sorted(set(MISSING_RE.findall(text)))


def ensure_builtin_templates(session: Session) -> None:
    existing = set(session.scalars(select(EmailTemplate.key)))
    for key, t in BUILTIN_TEMPLATES.items():
        if key not in existing:
            session.add(EmailTemplate(key=key, name=t["name"], kind=key, subject=t["subject"], body=t["body"], builtin=True))
    session.flush()


def build_context(session: Session, *, job: Job | None = None, contact: Contact | None = None,
                  resume_version: ResumeVersion | None = None, application=None, interview=None,
                  previous: EmailDraft | None = None) -> dict[str, Any]:
    sink: set[str] = set()

    def v(name: str, value: Any) -> Any:
        return value if value not in (None, "", []) else Missing(name, sink)

    p = profile_svc.get_profile(session)
    confirmed = p.confirmed_at is not None
    full_name = (p.full_name or " ".join(x for x in (p.first_name, p.last_name) if x)) if confirmed else None
    have = profile_svc.confirmed_skills(session)
    job_skills = []
    if job and job.match_details:
        s = job.match_details.get("skills") or {}
        job_skills = [x for x in (s.get("mandatory") or []) + (s.get("preferred") or []) if x in have]
    links = [f"{l.label}: {l.url}" for l in session.scalars(select(ProfileLink).where(ProfileLink.status == "confirmed"))]
    contact_first = None
    if contact and contact.name and contact.name.lower() not in ("recruiting team", "hiring team"):
        contact_first = contact.name.split()[0]
    ctx = {
        "candidate": {
            "full_name": v("candidate.full_name (confirm your profile)", full_name),
            "first_name": v("candidate.first_name", (p.first_name or (full_name or "").split(" ")[0]) if confirmed else None),
            "email": p.email if confirmed else None,
            "phone": p.phone if confirmed else None,
            "headline": p.headline if confirmed else None,
            "links": links,
            "matching_skills": ", ".join(job_skills[:5]),
        },
        "contact": {
            "first_name": contact_first or Missing("contact.first_name", sink),
            "name": v("contact.name", contact.name if contact else None),
            "title": v("contact.title", contact.title if contact else None),
        },
        "company": {"name": v("company.name", (job.company_name if job else None) or
                              (contact.company.name if contact and contact.company else None))},
        "job": {"title": v("job.title", job.title if job else None),
                "url": (job.source_url or job.apply_url) if job else None,
                "location": job.location if job else None},
        "resume": {"label": resume_version.label if resume_version else None},
        "application": {"applied_on": v("application.applied_on",
                                        application.applied_at.strftime("%d %b %Y") if application and getattr(application, "applied_at", None) else None)},
        "interview": {"date": v("interview.date", interview.scheduled_at.strftime("%d %b %Y")
                                if interview and getattr(interview, "scheduled_at", None) else None),
                      "round": v("interview.round", getattr(interview, "round_name", None) if interview else None)},
        "previous": {"subject": v("previous.subject", previous.subject if previous else None),
                     "sent_on": v("previous.sent_on", previous.sent_at.strftime("%d %b %Y")
                                  if previous and previous.sent_at else None)},
    }
    return ctx


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    template_key: str
    job_id: int | None = None
    contact_id: int | None = None
    application_id: int | None = None
    interview_id: int | None = None
    resume_version_id: int | None = None


class DraftEdit(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    to_address: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class DraftError(ValueError):
    pass


def _related(session: Session, data: DraftCreate):
    from jobpilot.services.registry import get_application, get_interview

    job = session.get(Job, data.job_id) if data.job_id else None
    contact = session.get(Contact, data.contact_id) if data.contact_id else None
    application = get_application(session, data.application_id)
    interview = get_interview(session, data.interview_id)
    if application is not None and job is None:
        job = application.job
    if interview is not None and application is None:
        application = interview.application
        job = job or application.job
    return job, contact, application, interview


def create_draft(session: Session, data: DraftCreate, previous: EmailDraft | None = None) -> EmailDraft:
    ensure_builtin_templates(session)
    tpl = session.scalar(select(EmailTemplate).where(EmailTemplate.key == data.template_key, EmailTemplate.active.is_(True)))
    if tpl is None:
        raise DraftError("Unknown template")
    job, contact, application, interview = _related(session, data)
    if contact is not None:
        if contact.do_not_contact:
            raise DraftError(f"{contact.display_name} is marked do-not-contact")
        blocked = contact_svc.is_blocked(session, contact.email, contact.company_id)
        if blocked:
            raise DraftError(blocked)
    if tpl.kind == "referral_request":
        if contact is None or contact.role_type != "referral" or not contact.relationship_note:
            raise DraftError("Referral requests need a referral contact whose relationship you have described")
    resume_version = session.get(ResumeVersion, data.resume_version_id) if data.resume_version_id else None
    if resume_version is None and application is not None and getattr(application, "resume_version_id", None):
        resume_version = session.get(ResumeVersion, application.resume_version_id)
    if resume_version is None and job is not None and tpl.kind in OUTREACH_KINDS:
        from jobpilot.services.resumes import select_for_job

        resume_version = select_for_job(session, job)["version"]
    if resume_version is not None and resume_version.status != "approved":
        raise DraftError("Only approved resume versions can be attached")

    ctx = build_context(session, job=job, contact=contact, resume_version=resume_version, application=application,
                        interview=interview, previous=previous)
    subject, m1 = render(tpl.subject, ctx)
    body, m2 = render(tpl.body, ctx)
    to = contact.email if contact else None
    draft = EmailDraft(kind=tpl.kind, template_id=tpl.id, contact_id=contact.id if contact else None,
                       job_id=job.id if job else None, resume_version_id=resume_version.id if resume_version else None,
                       to_address=to, subject=subject.strip(), body=body.strip(), status="draft",
                       missing_fields=sorted(set(m1 + m2)), follow_up_of_id=previous.id if previous else None)
    if application is not None:
        draft.application_id = application.id
    session.add(draft)
    session.flush()
    if contact is not None and job is not None:
        contact_svc.link_contact_job(session, contact, job, source=f"email draft #{draft.id}")
    audit.record(session, "draft.created", "email_draft", draft.id, kind=tpl.kind, missing=len(draft.missing_fields))
    return draft


def edit_draft(session: Session, draft: EmailDraft, data: DraftEdit) -> EmailDraft:
    from jobpilot.models import DraftRevision

    if draft.status in ("sent", "cancelled"):
        raise DraftError(f"Cannot edit a {draft.status} draft")
    new_to = data.to_address.lower() if data.to_address else None
    if (new_to, data.subject, data.body) != (draft.to_address, draft.subject, draft.body):
        n = len(list(session.scalars(select(DraftRevision.id).where(DraftRevision.draft_id == draft.id)))) + 1
        session.add(DraftRevision(draft_id=draft.id, revision_no=n, to_address=draft.to_address, subject=draft.subject,
                                  body=draft.body, status_before=draft.status))
    draft.to_address = data.to_address.lower() if data.to_address else None
    draft.subject, draft.body = data.subject, data.body
    draft.missing_fields = sorted(set(MISSING_RE.findall(data.subject + "\n" + data.body)))
    if draft.status == "approved":
        draft.status = "draft"  # material change: needs re-approval
        draft.approved_at = None
    audit.record(session, "draft.edited", "email_draft", draft.id)
    return draft


def approval_problems(session: Session, draft: EmailDraft) -> list[str]:
    problems = []
    if draft.missing_fields:
        problems.append("fill in: " + ", ".join(draft.missing_fields))
    if not draft.to_address:
        problems.append("recipient address is empty")
    blocked = contact_svc.is_blocked(session, draft.to_address,
                                     draft.contact.company_id if draft.contact else None)
    if blocked:
        problems.append(blocked)
    if draft.contact and draft.contact.do_not_contact:
        problems.append(f"{draft.contact.display_name} is marked do-not-contact")
    if draft.follow_up_of_id:
        parent = session.get(EmailDraft, draft.follow_up_of_id)
        if parent and parent.reply_received_at:
            problems.append("a reply was already received on this thread")
    return problems


def recipient_warnings(draft: EmailDraft) -> list[str]:
    c = draft.contact
    if not draft.to_address:
        return []
    if c is None or (c.email or "").lower() != draft.to_address.lower():
        return ["recipient address is not linked to a contact record — check it carefully"]
    if c.email_status == "guessed":
        return ["this address is GUESSED, not verified — it may reach the wrong person"]
    if c.email_status == "unverified":
        return [f"address is unverified (source: {c.source_type}{', ' + c.source_url if c.source_url else ''})"]
    return []


def approve(session: Session, draft: EmailDraft, acknowledge_guessed: bool = False) -> EmailDraft:
    if draft.status != "draft":
        raise DraftError(f"Draft is {draft.status}")
    problems = approval_problems(session, draft)
    if problems:
        raise DraftError("Cannot approve: " + "; ".join(problems))
    if draft.contact and draft.contact.email_status == "guessed" and not acknowledge_guessed:
        raise DraftError("Recipient address is guessed; tick the acknowledgement to approve anyway")
    draft.status = "approved"
    draft.approved_at = utcnow()
    audit.record(session, "draft.approved", "email_draft", draft.id)
    return draft


def mark_sent(session: Session, draft: EmailDraft, via: str = "manual", when=None) -> EmailDraft:
    """Record that an approved draft was sent (by you, or via Gmail)."""
    if draft.status != "approved":
        raise DraftError("Only approved drafts can be marked as sent")
    cfg = settings_store.get(session, "outreach")
    draft.status = "sent"
    draft.sent_at = when or utcnow()
    draft.sent_via = via
    if draft.kind in OUTREACH_KINDS or draft.kind == "follow_up":
        chain = followup_chain_length(session, draft)
        if chain < cfg["max_follow_ups"]:
            draft.follow_up_due_at = draft.sent_at + timedelta(days=cfg["follow_up_days"])
    if draft.contact:
        contact_svc.log_interaction(session, draft.contact, "email", "outbound",
                                    f"Sent: {draft.subject}", draft_id=draft.id)
    audit.record(session, "draft.sent", "email_draft", draft.id, via=via)
    return draft


def cancel(session: Session, draft: EmailDraft) -> None:
    if draft.status == "sent":
        raise DraftError("Sent messages cannot be cancelled")
    draft.status = "cancelled"
    draft.follow_up_due_at = None
    audit.record(session, "draft.cancelled", "email_draft", draft.id)


def revisions(session: Session, draft: EmailDraft) -> list:
    from jobpilot.models import DraftRevision

    return list(session.scalars(select(DraftRevision).where(DraftRevision.draft_id == draft.id)
                                .order_by(DraftRevision.revision_no.desc())))


def thread_root(session: Session, draft: EmailDraft) -> EmailDraft:
    seen = set()
    while draft.follow_up_of_id and draft.id not in seen:
        seen.add(draft.id)
        draft = session.get(EmailDraft, draft.follow_up_of_id)
    return draft


def thread(session: Session, draft: EmailDraft) -> list[EmailDraft]:
    root = thread_root(session, draft)
    out, frontier = [root], [root.id]
    while frontier:
        children = list(session.scalars(select(EmailDraft).where(EmailDraft.follow_up_of_id.in_(frontier))))
        out.extend(children)
        frontier = [c.id for c in children]
    return out


def followup_chain_length(session: Session, draft: EmailDraft) -> int:
    return sum(1 for d in thread(session, draft) if d.kind == "follow_up" and d.status != "cancelled")


def record_reply(session: Session, draft: EmailDraft, summary: str | None = None, when=None) -> None:
    """A reply arrived: suppress all pending follow-ups in this thread."""
    when = when or utcnow()
    for d in thread(session, draft):
        d.reply_received_at = d.reply_received_at or when
        if d.follow_up_due_at:
            d.follow_up_due_at = None
            d.suppressed_reason = "reply received"
        if d.status in ("draft", "approved") and d.kind == "follow_up":
            d.status = "cancelled"
            d.suppressed_reason = "reply received"
    if draft.contact:
        contact_svc.log_interaction(session, draft.contact, "email", "inbound", summary or "Reply received",
                                    draft_id=draft.id)
        if draft.contact.email_status != "verified" and draft.contact.email and draft.to_address \
                and draft.contact.email.lower() == draft.to_address.lower():
            contact_svc.verify_email(session, draft.contact, f"reply received to draft #{draft.id}")
    audit.record(session, "draft.reply_recorded", "email_draft", draft.id)


def followup_suppression_reason(session: Session, draft: EmailDraft) -> str | None:
    if draft.reply_received_at:
        return "reply received"
    if draft.contact and draft.contact.do_not_contact:
        return "contact is do-not-contact"
    if contact_svc.is_blocked(session, draft.to_address, draft.contact.company_id if draft.contact else None):
        return "recipient on do-not-contact list"
    from jobpilot.services.registry import application_closed_reason

    reason = application_closed_reason(session, draft)
    if reason:
        return reason
    if any(d.kind == "follow_up" and d.status in ("draft", "approved") for d in thread(session, draft)):
        return "a follow-up is already drafted"
    return None


def due_followups(session: Session, apply_suppression: bool = False) -> list[EmailDraft]:
    """Sent messages whose follow-up date has passed and that still warrant a follow-up.

    Read-only by default (safe for page views). With ``apply_suppression=True`` (scheduled
    follow-up check) messages that no longer warrant a follow-up get their due date cleared
    and the reason recorded.
    """
    out = []
    for d in session.scalars(select(EmailDraft).where(EmailDraft.status == "sent", EmailDraft.follow_up_due_at.is_not(None),
                                                      EmailDraft.follow_up_due_at <= utcnow())):
        reason = followup_suppression_reason(session, d)
        if reason == "a follow-up is already drafted":
            continue
        if reason:
            if apply_suppression:
                d.follow_up_due_at = None
                d.suppressed_reason = reason
            continue
        out.append(d)
    return out


def create_followup(session: Session, draft: EmailDraft) -> EmailDraft:
    reason = followup_suppression_reason(session, draft)
    if reason:
        raise DraftError(f"Follow-up not appropriate: {reason}")
    cfg = settings_store.get(session, "outreach")
    if followup_chain_length(session, draft) >= cfg["max_follow_ups"]:
        raise DraftError(f"Already sent {cfg['max_follow_ups']} follow-up(s) on this thread")
    fu = create_draft(session, DraftCreate(template_key="follow_up", job_id=draft.job_id, contact_id=draft.contact_id,
                                           application_id=getattr(draft, "application_id", None),
                                           resume_version_id=draft.resume_version_id), previous=draft)
    fu.to_address = draft.to_address
    fu.gmail_thread_id = draft.gmail_thread_id
    draft.follow_up_due_at = None
    return fu
