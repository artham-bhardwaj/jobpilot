"""Is a job a fresher / SDE-1 role? One of ``fit``, ``unstated`` or ``no``, stored on the job as ``level_fit``.

- ``no``: the title names a higher level (senior, lead, staff, SDE II / Engineer 2 / III, intermediate, mid-level,
  experienced, manager …), it is an internship, or the posting asks for more than 2 years.
- ``fit``: the title says entry level (junior, new grad, graduate, fresher, trainee, associate, SDE I / SDE-1,
  Engineer I, early career) or the posting asks for at most 2 years (0 included).
- ``unstated``: neither — a plain "Software Engineer" with no years given. Shown by default, marked as unstated.

Only the title and the stated years are used, never guessed from the rest of the description.
"""

from __future__ import annotations

import re

from jobpilot.services.job_parser import ENTRY_TITLE, SENIOR_TITLE

MAX_FRESHER_YEARS = 2.0
VALUES = ("fit", "unstated", "no")

# Levels above SDE I that SENIOR_TITLE doesn't cover: "SDE II", "SDE-2", "SDE2", "Engineer 2", "Software Engineer II",
# "L4", "Intermediate", "Mid-level", "Experienced".
_ABOVE_ENTRY = re.compile(r"\b(ii|2|sde[- ]?(?:ii|2|3)|l4|intermediate|mid[- ]?level|mid[- ]?senior|mid|experienced|expert)\b", re.I)
_INTERN = re.compile(r"\b(intern|internship|apprentice(?:ship)?)\b", re.I)


def classify(title: str | None, min_years: float | None) -> str:
    title = title or ""
    if SENIOR_TITLE.search(title) or _ABOVE_ENTRY.search(title) or _INTERN.search(title):
        return "no"
    if min_years is not None and min_years > MAX_FRESHER_YEARS:
        return "no"
    if ENTRY_TITLE.search(title) or min_years is not None:
        return "fit"
    return "unstated"


def of(job) -> str:
    return getattr(job, "level_fit", None) or classify(job.title, job.experience_min_years)


LABELS = {"fit": "Fresher / SDE-1", "unstated": "level not stated", "no": "above SDE-1"}


def backfill(session, only_missing: bool = True) -> int:
    """Set ``level_fit`` on jobs (all, or those without it). Returns how many changed."""
    from sqlalchemy import select

    from jobpilot.models import Job

    stmt = select(Job)
    if only_missing:
        stmt = stmt.where(Job.level_fit.is_(None))
    n = 0
    for job in session.scalars(stmt):
        value = classify(job.title, job.experience_min_years)
        if job.level_fit != value:
            job.level_fit = value
            n += 1
    return n
