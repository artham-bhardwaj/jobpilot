"""Turn a hiring post you copied from LinkedIn into a job lead, a contact and an email draft.

You paste the post's text (and optionally its link). JobPilot never visits LinkedIn: the link is only stored as the
source. The email address must be one the poster published in the post. The draft follows the normal flow: you review
and approve it, and it is sent within the daily limit and the do-not-contact rules.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jobpilot.services import audit
from jobpilot.services import contacts as contact_svc
from jobpilot.services import drafts as draft_svc
from jobpilot.services import jobs as job_svc
from jobpilot.services.profile import EMAIL_PATTERN

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# "jane [at] acme [dot] com", "jane(at)acme(dot)com", "jane {at} acme . com"
_AT = re.compile(r"\s*[\[({<]\s*at\s*[\])}>]\s*", re.I)
_DOT = re.compile(r"\s*[\[({<]\s*dot\s*[\])}>]\s*", re.I)
# LinkedIn UI text that comes along when a post is copied
_NOISE = re.compile(r"^(feed post( number \d+)?|promoted|follow|\+ follow|like|comment|repost|send|see more|"
                    r"…\s*more|\d+[smhdwy]o? ?•.*|•\s*(1st|2nd|3rd\+?|you)|(1st|2nd|3rd\+?)|\d+ (comments?|reposts?)|"
                    r"visible to anyone.*|edited)$", re.I)
_NAME = re.compile(r"^[A-Z][a-zA-Z'’.-]+(?: [A-Z][a-zA-Z'’.-]*){1,3}$")
_ROLE_WORDS = r"(?:engineers?|developers?|SDE(?:[- ]?(?:I{1,3}|[1-3]))?s?|programmers?|architects?|interns?|leads?)"
_ROLE = re.compile(
    r"(?:hiring|looking for|seeking|openings? for|opening:|positions?:|roles?:|role of|for the role of)\s*:?\s*"
    r"(?:an?\s+|for\s+(?:an?\s+)?|multiple\s+|\d+\s+)?"
    r"((?:[A-Z0-9][\w/+#.()-]*\s+){0,5}" + _ROLE_WORDS + r")\b", re.I)
_ROLE_LINE = re.compile(r"\b" + _ROLE_WORDS + r"\b", re.I)
_COMPANY = [
    re.compile(r"(?im)^\s*(?:company|organi[sz]ation|employer)\s*[:\-–]\s*@?([A-Z][\w&.'-]*(?:[ \t]+[A-Z][\w&.'-]*){0,3})"),
    re.compile(r"\b(?:we at|here at|team at|join(?: us at| our team at)?|hiring at|opportunit(?:y|ies) at|"
               r"openings? at|positions? at|roles? at)\s+@?([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"),
    re.compile(r"\b@?([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})\s+is\s+(?:actively\s+)?hiring\b"),
]
_HEADLINE_AT = re.compile(r"\bat\s+@?([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})")
_RECRUITER = re.compile(r"recruit|talent|\bhr\b|human resources|people (team|partner)|sourc(er|ing)|hiring partner", re.I)
_NO_EMAIL = re.compile(r"(do not|don'?t|please don'?t|no)\s+(e-?mail|cold e-?mail|e-?mails)|dm (me )?only|"
                       r"apply (only )?(via|through) the (link|portal)|only applications? (via|through)", re.I)
_LOCATION = re.compile(r"\b(?:location|based in|work from|office in)\s*[:\-–]?\s*([A-Z][\w ,/()-]{1,60})", re.I)
_WORD_STOP = {"Our", "The", "We", "Us", "This", "Team", "LinkedIn", "Hiring", "Join", "My", "Your", "A", "An"}


def clean_post_url(url: str | None) -> str | None:
    """The post's link without tracking parameters, or None. Only linkedin.com links are accepted."""
    url = (url or "").strip()
    if not url:
        return None
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise ValueError("The post link must be a linkedin.com link (or leave it empty)")
    return urlunsplit(("https", "www.linkedin.com", parts.path.rstrip("/") or "/", "", ""))


def _deobfuscate(text: str) -> str:
    return _DOT.sub(".", _AT.sub("@", text))


def _company_from_domain(email: str) -> str | None:
    dom = contact_svc.email_domain(email)
    if not dom or dom in contact_svc.PERSONAL_DOMAINS:
        return None
    labels = dom.split(".")
    core = labels[-3] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac") else labels[-2]
    return core.capitalize() if core else None


def _clean_company(name: str | None) -> str | None:
    if not name:
        return None
    words = name.strip(" .,'-").split()
    while words and words[-1] in _WORD_STOP | {"is", "for", "and"}:
        words.pop()
    if not words or words[0] in _WORD_STOP:
        return None
    return " ".join(words)[:200] or None


def parse_post(text: str) -> dict[str, Any]:
    """Best-effort fields from a pasted post. Everything is shown to you for editing before anything is saved."""
    raw = text or ""
    body = _deobfuscate(raw)
    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln and not _NOISE.match(ln)]

    emails: list[str] = []
    for e in _EMAIL.findall(body):
        e = e.lower().rstrip(".")
        if not re.search(r"\.(png|jpe?g|gif|svg|webp)$", e) and e not in emails:
            emails.append(e)

    poster_name = poster_title = None
    for i, ln in enumerate(lines[:6]):
        if _NAME.match(ln) and not re.search(r"hiring|job|opening|role", ln, re.I):
            poster_name = ln
            for nxt in lines[i + 1:i + 4]:
                if nxt != ln and not _NAME.match(nxt) and len(nxt) <= 160:
                    poster_title = nxt
                    break
            break

    role = None
    m = _ROLE.search(body)
    if m:
        role = re.sub(r"\s+", " ", m.group(1)).strip(" .,:-")
    if not role:
        for ln in lines:
            if _ROLE_LINE.search(ln) and len(ln) <= 90 and ln != poster_title:
                role = re.sub(r"^[#•*\-–\s]+|[!.:]+$", "", ln).strip()
                break

    company = None
    for rx in _COMPANY:
        m = rx.search(body)
        if m and (company := _clean_company(m.group(1))):
            break
    if not company and poster_title:
        m = _HEADLINE_AT.search(poster_title)
        company = _clean_company(m.group(1)) if m else None
    if not company:
        company = next((c for c in map(_company_from_domain, emails) if c), None)

    loc = _LOCATION.search(body)
    title_text = f"{poster_title or ''}"
    warnings = []
    if not emails:
        warnings.append("The post has no email address. Message the poster on LinkedIn instead, or add an address "
                        "they published elsewhere.")
    if _NO_EMAIL.search(body):
        warnings.append("The post seems to ask people not to email (for example 'DM only' or 'apply via the link'). "
                        "Respect that before sending anything.")
    return {
        "emails": emails,
        "email": emails[0] if emails else None,
        "poster_name": poster_name,
        "poster_title": poster_title,
        "company_name": company,
        "role_title": role[:300] if role else None,
        "location": loc.group(1).strip(" .,")[:300] if loc else None,
        "role_type": "recruiter" if _RECRUITER.search(title_text) else "hiring_manager",
        "warnings": warnings,
    }


class PostLeadIn(BaseModel):
    """What you confirmed on the review form."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    post_text: str = Field(min_length=20, max_length=20_000)
    post_url: str | None = Field(default=None, max_length=1000)
    poster_name: str | None = Field(default=None, max_length=200)
    poster_title: str | None = Field(default=None, max_length=200)
    company_name: str = Field(min_length=1, max_length=200)
    role_title: str = Field(min_length=1, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    role_type: Literal["recruiter", "hiring_manager"] = "recruiter"
    template_key: Literal["recruiter_outreach", "hiring_manager"] = "recruiter_outreach"
    email_in_post: bool = False  # you confirmed the poster published this address

    @field_validator("post_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return clean_post_url(v)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str | None) -> str | None:
        return v.lower() if v else v


def create_lead(session, data: PostLeadIn) -> dict[str, Any]:
    """Save the job lead and the contact, and draft an email if there is an address. Nothing is sent."""
    if data.email and not data.email_in_post:
        raise draft_svc.DraftError("Tick the box confirming the poster published this email address in the post")
    if data.email:
        company = job_svc.get_or_create_company(session, data.company_name)
        blocked = contact_svc.is_blocked(session, data.email, company.id if company else None)
        if blocked:
            raise draft_svc.DraftError(blocked)

    job, _ = job_svc.add_manual(session, job_svc.ManualJobIn(
        title=data.role_title, company_name=data.company_name, location=data.location,
        description_text=data.post_text, source_url=data.post_url))
    contact, _ = contact_svc.save_contact(session, contact_svc.ContactIn(
        name=data.poster_name, title=data.poster_title, company_name=data.company_name, job_id=job.id,
        role_type=data.role_type, email=data.email, source_type="linkedin_manual", source_url=data.post_url,
        source_note="address published by the poster in their LinkedIn hiring post (pasted by you)" if data.email
        else "author of a LinkedIn hiring post (pasted by you)"))
    draft = None
    if data.email:
        draft = draft_svc.create_draft(session, draft_svc.DraftCreate(
            template_key=data.template_key, job_id=job.id, contact_id=contact.id))
    audit.record(session, "hiring_post.imported", "job", job.id, contact_id=contact.id,
                 draft_id=draft.id if draft else None)
    return {"job": job, "contact": contact, "draft": draft}
