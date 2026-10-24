"""Answer bank and per-application questions.

- Answers are written by you. A bank answer is *suggested* for an application question only when it is
  approved, not sensitive, applies to the job's role category, and the wording is similar enough.
- Sensitive questions (legal declarations, work authorisation, sponsorship, disability, demographics,
  salary declarations, criminal history, consent) are never suggested or filled from the bank; you must
  answer them yourself for each application.
- Nothing here submits anything or claims a form was completed.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import AnswerBankEntry, Application, ApplicationQuestion, utcnow
from jobpilot.models.applications import ANSWER_CATEGORIES
from jobpilot.services import audit
from jobpilot.services.text_utils import normalize

SIMILARITY_THRESHOLD = 0.6
SENSITIVE_PATTERNS = [
    ("work authorisation / visa", r"authori[sz]ed to work|work authori[sz]ation|right to work|eligible to work|visa|sponsor|citizen|nationality|immigration"),
    ("legal declaration / consent", r"declar|certify|attest|consent|agree|acknowledg|terms|privacy|truthful|accurate to the best"),
    ("disability", r"disabilit|accommodation|impairment"),
    ("demographics", r"gender|sex\b|race|ethnic|veteran|sexual orientation|pronoun|religio|caste|marital|date of birth|\bdob\b|\bage\b|transgender|nationality"),
    ("salary declaration", r"salary|compensation|\bctc\b|pay expectation|expected pay|current pay|remuneration"),
    ("criminal / background", r"criminal|convict|background check|offen[cs]e|felony"),
]
COMMON_QUESTIONS = [
    ("Why do you want to work at this company?", "motivation"),
    ("Why are you interested in this role?", "motivation"),
    ("Tell us about a project you are proud of.", "experience"),
    ("Describe your experience with backend development.", "experience"),
    ("What is your notice period?", "logistics"),
    ("Are you willing to relocate?", "logistics"),
    ("When can you start?", "logistics"),
    ("How did you hear about this position?", "general"),
]
_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "you", "your", "are", "is", "do", "did", "with",
         "this", "that", "what", "why", "how", "please", "describe", "tell", "us", "about", "have", "any", "we", "our", "be"}


# Attestations only you can make: never filled by JobPilot, never answered from Telegram, never reused.
LEGAL_RX = re.compile(r"declar|certify|attest|consent|\bagree|acknowledg|terms|privacy (?:notice|policy)|truthful|"
                      r"accurate to the best|\bnda\b|non-disclosure|signature|\bsign\b|e-sign|"
                      r"typ(?:e|ing) your (?:full |legal )?name", re.I)


def is_legal(question: str | None) -> bool:
    return bool(LEGAL_RX.search(question or ""))


def resolve_from_form(session: Session, app: Application, verified_labels: list[str]) -> int:
    """Open questions whose field JobPilot has now filled and verified from your confirmed profile are closed
    (status ``resolved``, not ``approved``: you did not answer them, your profile did)."""
    labels = [normalize(re.sub(r"\s*\*\s*$", "", x)) for x in verified_labels if x]
    n = 0
    for q in session.scalars(select(ApplicationQuestion).where(ApplicationQuestion.application_id == app.id,
                                                               ApplicationQuestion.status == "unanswered")):
        text = normalize(q.question)
        if any(lab and (text == lab or text.startswith(lab + " ")) for lab in labels) and not is_legal(q.question):
            q.status, q.answer_source = "resolved", "profile (filled and verified on the form)"
            n += 1
    if n:
        session.flush()
        audit.record(session, "application.questions_resolved_by_profile", "application", app.id, count=n)
    return n


def sensitive_reason(question: str) -> str | None:
    for label, rx in SENSITIVE_PATTERNS:
        if re.search(rx, question or "", re.I):
            return label
    return None


def _tokens(text: str) -> set[str]:
    return {t for t in normalize(text).replace("/", " ").split() if t not in _STOP and len(t) > 1}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def save_entry(session: Session, question: str, answer: str, category: str = "general",
               role_categories: list[str] | None = None, approved: bool = False,
               entry_id: int | None = None) -> AnswerBankEntry:
    question, answer = (question or "").strip(), (answer or "").strip()
    if not question or not answer:
        raise ValueError("Question and answer are both required")
    if category not in ANSWER_CATEGORIES:
        raise ValueError("Unknown category")
    reason = sensitive_reason(question)
    entry = session.get(AnswerBankEntry, entry_id) if entry_id else AnswerBankEntry()
    if entry is None:
        raise LookupError("Answer not found")
    entry.question, entry.answer = question[:2000], answer[:10000]
    entry.category = "sensitive" if reason else category
    entry.sensitive = bool(reason)
    entry.role_categories = [c for c in (role_categories or []) if c] or None
    entry.approved = bool(approved)
    if entry_id is None:
        session.add(entry)
    session.flush()
    audit.record(session, "answers.saved", "answer_bank", entry.id, sensitive=entry.sensitive, approved=entry.approved)
    return entry


def suggest(session: Session, question: str, role_category: str | None) -> dict[str, Any] | None:
    """Best approved, non-sensitive bank answer for this question and context, or None."""
    if sensitive_reason(question):
        return None
    best, best_sim = None, 0.0
    for e in session.scalars(select(AnswerBankEntry).where(AnswerBankEntry.approved.is_(True),
                                                           AnswerBankEntry.sensitive.is_(False))):
        if e.role_categories and role_category not in e.role_categories:
            continue
        sim = similarity(question, e.question)
        if sim > best_sim:
            best, best_sim = e, sim
    if best is None or best_sim < SIMILARITY_THRESHOLD:
        return None
    return {"entry": best, "similarity": round(best_sim, 2)}


def add_questions(session: Session, app: Application, texts: list[str], source: str = "manual") -> list[ApplicationQuestion]:
    existing = {normalize(q.question) for q in session.scalars(select(ApplicationQuestion)
                                                                .where(ApplicationQuestion.application_id == app.id))}
    added = []
    for raw in texts:
        text = re.sub(r"\s+", " ", (raw or "").replace("[you answer]", "")).strip(" *")
        if len(text) < 3 or normalize(text) in existing:
            continue
        q = ApplicationQuestion(application_id=app.id, question=text[:2000], source=source,
                                sensitive=bool(sensitive_reason(text)), status="unanswered")
        session.add(q)
        existing.add(normalize(text))
        added.append(q)
    session.flush()
    if added:
        audit.record(session, "application.questions_added", "application", app.id, count=len(added), source=source)
    return added


def questions(session: Session, app: Application) -> list[dict[str, Any]]:
    out = []
    for q in session.scalars(select(ApplicationQuestion).where(ApplicationQuestion.application_id == app.id)
                             .order_by(ApplicationQuestion.id)):
        out.append({"q": q, "sensitive_reason": sensitive_reason(q.question),
                    "suggestion": None if q.status == "approved" else suggest(session, q.question, app.job.category)})
    return out


def set_answer(session: Session, q: ApplicationQuestion, text: str, *, from_bank: AnswerBankEntry | None = None) -> None:
    if from_bank is not None:
        if q.sensitive or from_bank.sensitive or not from_bank.approved:
            raise ValueError("Sensitive questions must be answered by you, not from the answer bank")
        text, q.answer_source = from_bank.answer, f"bank:{from_bank.id}"
        from_bank.last_used_at = utcnow()
    else:
        q.answer_source = "manual"
    q.answer = (text or "").strip()[:10000] or None
    q.status, q.approved_at = "unanswered", None  # any change needs (re)approval
    audit.record(session, "application.answer_set", "application", q.application_id, question_id=q.id,
                 source=q.answer_source)


def approve(session: Session, q: ApplicationQuestion, answered_myself: bool = False) -> None:
    if not q.answer:
        raise ValueError("Write an answer first")
    if q.sensitive and not answered_myself:
        raise ValueError("This is a sensitive question — confirm that you answered it yourself")
    q.status, q.approved_at = "approved", utcnow()
    audit.record(session, "application.answer_approved", "application", q.application_id, question_id=q.id,
                 sensitive=q.sensitive)


def unanswered_count(session: Session, app: Application) -> int:
    return len(list(session.scalars(select(ApplicationQuestion.id).where(
        ApplicationQuestion.application_id == app.id, ApplicationQuestion.status == "unanswered"))))
