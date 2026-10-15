"""Skill-gap analysis and the learning backlog.

Compares skills required by your relevant/target jobs with your *confirmed* skills.
Progress can be tracked freely, but a skill only becomes ``confirmed`` when you
explicitly confirm it with evidence.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobpilot.models import Application, Job, LearningItem, Skill, utcnow
from jobpilot.services import audit, settings_store
from jobpilot.services.skills_taxonomy import category_of


def analyze(session: Session, scope: str = "relevant", category: str | None = None) -> dict:
    threshold = settings_store.get(session, "matching")["relevance_threshold"]
    stmt = select(Job).where(Job.status == "active", Job.match_details.is_not(None))
    applied_ids = select(Application.job_id)
    if scope == "relevant":
        stmt = stmt.where(or_(Job.relevance_score >= threshold, Job.user_status == "shortlisted", Job.id.in_(applied_ids)))
    elif scope == "shortlisted":
        stmt = stmt.where(or_(Job.user_status == "shortlisted", Job.id.in_(applied_ids)))
    stmt = stmt.where(Job.user_status != "dismissed")
    if category:
        stmt = stmt.where(Job.category == category)
    jobs = list(session.scalars(stmt))
    statuses = {s.normalized_name: s.status for s in session.scalars(select(Skill))}
    backlog = {li.skill_name: li for li in session.scalars(select(LearningItem))}
    stats: dict[str, dict] = defaultdict(lambda: {"mandatory": 0, "preferred": 0, "jobs": [], "relevance": 0.0,
                                                  "categories": set()})
    for job in jobs:
        sk = (job.match_details or {}).get("skills") or {}
        for kind in ("mandatory", "preferred"):
            for s in sk.get(kind) or []:
                st = stats[s]
                st[kind] += 1
                st["relevance"] += job.relevance_score or 0
                if len(st["jobs"]) < 5:
                    st["jobs"].append((job.id, job.title, job.company_name))
                if job.category:
                    st["categories"].add(job.category)
    rows = []
    for skill, st in stats.items():
        count = st["mandatory"] + st["preferred"]
        status = statuses.get(skill, "missing")
        priority = round((2 * st["mandatory"] + st["preferred"]) * (st["relevance"] / count / 100 if count else 0), 2)
        rows.append({"skill": skill, "category": category_of(skill), "status": status, "mandatory": st["mandatory"],
                     "preferred": st["preferred"], "jobs": count, "share": round(count / len(jobs), 2) if jobs else None,
                     "priority": priority, "examples": st["jobs"], "role_categories": sorted(st["categories"]),
                     "backlog": backlog.get(skill)})
    gaps = sorted([r for r in rows if r["status"] != "confirmed"], key=lambda r: -r["priority"])
    strengths = sorted([r for r in rows if r["status"] == "confirmed"], key=lambda r: -r["jobs"])
    return {"jobs_analyzed": len(jobs), "scope": scope, "gaps": gaps, "strengths": strengths}


class LearningUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    status: Literal["backlog", "in_progress", "practicing", "ready_for_confirmation", "dropped"]
    progress_pct: int = Field(default=0, ge=0, le=100)
    target_date: str | None = None
    resources: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)


def add_to_backlog(session: Session, skill_name: str, priority: float | None = None) -> LearningItem:
    item = session.scalar(select(LearningItem).where(LearningItem.skill_name == skill_name))
    if item is None:
        item = LearningItem(skill_name=skill_name, priority=priority, status="backlog")
        session.add(item)
    elif item.status in ("dropped", "done"):
        item.status = "backlog"
    item.priority = priority if priority is not None else item.priority
    skill = session.scalar(select(Skill).where(Skill.normalized_name == skill_name))
    if skill is None:
        session.add(Skill(name=skill_name, normalized_name=skill_name, category=category_of(skill_name),
                          status="learning", source="learning backlog"))
    elif skill.status == "unverified":
        skill.status = "learning"
    session.flush()
    audit.record(session, "learning.added", "learning_item", item.id, skill=skill_name)
    return item


def update(session: Session, item: LearningItem, data: LearningUpdate) -> LearningItem:
    from datetime import date

    item.status = data.status
    # Progress is self-reported; 100% is reserved for confirmed mastery.
    item.progress_pct = min(data.progress_pct, 95)
    item.target_date = date.fromisoformat(data.target_date) if data.target_date else None
    item.resources, item.notes = data.resources, data.notes
    audit.record(session, "learning.updated", "learning_item", item.id, status=item.status)
    return item


def confirm_mastery(session: Session, item: LearningItem, evidence: str) -> Skill:
    """Explicit confirmation by you: marks the skill as confirmed, with evidence."""
    if not evidence or len(evidence.strip()) < 10:
        raise ValueError("Describe concrete evidence (e.g. project, certificate, work use) to confirm this skill")
    skill = session.scalar(select(Skill).where(Skill.normalized_name == item.skill_name))
    if skill is None:
        skill = Skill(name=item.skill_name, normalized_name=item.skill_name, category=category_of(item.skill_name))
        session.add(skill)
    skill.status, skill.confirmed_at = "confirmed", utcnow()
    skill.evidence = ((skill.evidence or "") + "\n" + evidence.strip()).strip()
    item.status, item.progress_pct, item.completed_at, item.evidence_of_mastery = "done", 100, utcnow(), evidence.strip()
    session.flush()
    audit.record(session, "learning.mastery_confirmed", "skill", skill.id, skill=skill.normalized_name)
    return skill
