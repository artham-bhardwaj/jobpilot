"""Late-bound lookups between modules (applications/interviews are defined in later stages).

Keeps the outreach module independent of the application pipeline.
"""

from __future__ import annotations

from sqlalchemy.orm import Session


def get_application(session: Session, application_id: int | None):
    if not application_id:
        return None
    try:
        from jobpilot.models import Application
    except ImportError:  # pragma: no cover - before Stage 4
        return None
    return session.get(Application, application_id)


def get_interview(session: Session, interview_id: int | None):
    if not interview_id:
        return None
    try:
        from jobpilot.models import Interview
    except ImportError:  # pragma: no cover - before Stage 5
        return None
    return session.get(Interview, interview_id)


def application_closed_reason(session: Session, draft) -> str | None:
    """Return why follow-ups are inappropriate for the draft's application, if any."""
    app = get_application(session, getattr(draft, "application_id", None))
    if app is None and getattr(draft, "job_id", None):
        try:
            from sqlalchemy import select

            from jobpilot.models import Application

            app = session.scalar(select(Application).where(Application.job_id == draft.job_id))
        except ImportError:  # pragma: no cover
            app = None
    if app is not None and app.status in ("rejected", "withdrawn", "offer"):
        return f"application is {app.status}"
    return None
