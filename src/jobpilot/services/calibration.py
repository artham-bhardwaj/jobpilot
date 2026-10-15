"""Matching calibration: inspect score components, record your judgements, preview weight changes.

Feedback is descriptive only. It is stored with a snapshot of the score and weights at the time,
and reported here; it never changes weights or rules by itself. Weights change only when you save them.
"""

from __future__ import annotations

import random
from statistics import mean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.models import Job, MatchFeedback
from jobpilot.services import audit, matching, settings_store
from jobpilot.services.settings_store import WEIGHT_KEYS

SAMPLE_MODES = ("recent", "top", "borderline", "random", "feedback")


def sample(session: Session, mode: str = "recent", n: int = 25, seed: int = 0) -> list[Job]:
    base = select(Job).where(Job.status == "active", Job.match_details.is_not(None))
    mcfg = settings_store.get(session, "matching")
    if mode == "top":
        q = base.order_by(Job.relevance_score.desc().nulls_last())
    elif mode == "borderline":
        q = base.where(Job.relevance_score >= mcfg["review_band_low"] - 10,
                       Job.relevance_score < mcfg["relevance_threshold"] + 10).order_by(Job.relevance_score.desc())
    elif mode == "feedback":
        q = base.where(Job.id.in_(select(MatchFeedback.job_id))).order_by(Job.id.desc())
    elif mode == "random":
        ids = list(session.scalars(select(Job.id).where(Job.status == "active", Job.match_details.is_not(None))))
        rng = random.Random(seed)
        chosen = rng.sample(ids, min(n, len(ids)))
        return list(session.scalars(select(Job).where(Job.id.in_(chosen)).order_by(Job.id)))
    else:
        q = base.order_by(Job.first_seen_at.desc())
    return list(session.scalars(q.limit(n)))


def preview(session: Session, jobs: list[Job], weights: dict[str, float]) -> dict[int, dict[str, Any]]:
    """Scores under proposed weights. Read-only: nothing is stored."""
    out = {}
    for job in jobs:
        r = matching.evaluate(session, job, weights=weights)
        out[job.id] = {"score": r["relevance"]["score"], "recommendation": r["recommendation"]["label"]}
    return out


def record_feedback(session: Session, job: Job, verdict: str, note: str | None = None) -> MatchFeedback:
    if verdict not in ("useful", "irrelevant"):
        raise ValueError("verdict must be 'useful' or 'irrelevant'")
    fb = MatchFeedback(job_id=job.id, verdict=verdict, note=(note or "").strip()[:2000] or None,
                       score=job.relevance_score, eligibility=job.eligibility,
                       weights=settings_store.get(session, "matching_weights"))
    session.add(fb)
    session.flush()
    audit.record(session, "matching.feedback", "job", job.id, verdict=verdict)
    return fb


def latest_feedback(session: Session) -> dict[int, MatchFeedback]:
    rows = session.scalars(select(MatchFeedback).order_by(MatchFeedback.id))
    return {fb.job_id: fb for fb in rows}


def report(session: Session) -> dict[str, Any]:
    """How your judgements compare with the current scores. Descriptive only; small samples are flagged."""
    threshold = settings_store.get(session, "matching")["relevance_threshold"]
    fb = latest_feedback(session)
    jobs = {j.id: j for j in session.scalars(select(Job).where(Job.id.in_(list(fb))))} if fb else {}
    useful = [jobs[i] for i, f in fb.items() if f.verdict == "useful" and i in jobs]
    irrelevant = [jobs[i] for i, f in fb.items() if f.verdict == "irrelevant" and i in jobs]

    def comp_means(js: list[Job]) -> dict[str, float | None]:
        out = {}
        for key in WEIGHT_KEYS:
            vals = [c["fraction"] for j in js for c in (j.match_details or {}).get("relevance", {}).get("components", [])
                    if c.get("key") == key]
            out[key] = round(mean(vals), 2) if vals else None
        return out

    return {
        "total": len(fb), "useful": len(useful), "irrelevant": len(irrelevant), "threshold": threshold,
        "enough_data": len(useful) >= 5 and len(irrelevant) >= 5,
        "mean_score_useful": round(mean(j.relevance_score or 0 for j in useful), 1) if useful else None,
        "mean_score_irrelevant": round(mean(j.relevance_score or 0 for j in irrelevant), 1) if irrelevant else None,
        "irrelevant_but_scored_relevant": [j for j in irrelevant if (j.relevance_score or 0) >= threshold],
        "useful_but_scored_below": [j for j in useful if (j.relevance_score or 0) < threshold],
        "component_means_useful": comp_means(useful), "component_means_irrelevant": comp_means(irrelevant),
        "feedback_count_all": session.scalar(select(func.count(MatchFeedback.id))) or 0,
    }


def save_weights(session: Session, weights: dict[str, float]) -> None:
    settings_store.validate("matching_weights", weights)
    old = settings_store.get(session, "matching_weights")
    settings_store.put(session, "matching_weights", weights)
    audit.record(session, "matching.weights_changed", "app_setting", None, old=old, new=weights)
