"""Job classification, relevance scoring and eligibility assessment.

Two independent results are produced for each job:

* **Relevance (0-100)** — how well the job matches what the candidate *wants*
  (role category, skills overlap, level, location/remote, keywords, salary).
* **Eligibility** — whether the candidate meets the *stated* requirements,
  using **confirmed** profile facts only:
    - ``eligible``         all key checks pass and nothing relevant is unknown
    - ``likely_eligible``  key checks pass; some secondary information unknown
    - ``unknown``          a key requirement or candidate fact is missing
    - ``ineligible``       at least one stated requirement is clearly not met
  Missing information never produces ``eligible``.

Every component records a human-readable explanation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Education, Job, utcnow
from jobpilot.services import job_parser, profile as profile_svc, settings_store
from jobpilot.services.text_utils import contains_term, normalize


def classify(title: str, description: str | None, categories: dict[str, dict]) -> dict[str, Any]:
    t = f" {normalize(title)} "
    d = normalize((description or "")[:6000])
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    for cid, cat in categories.items():
        s = 0.0
        r: list[str] = []
        title_hits = [k for k in cat.get("title_keywords", []) if contains_term(t, k)]
        if title_hits:
            s += 2.0 + 0.5 * (len(title_hits) - 1)
            r.append(f"title mentions {', '.join(title_hits)}")
        skill_hits = [k for k in cat.get("skill_keywords", []) if contains_term(d, k)]
        if skill_hits:
            s += min(1.5, 0.3 * len(skill_hits))
            r.append(f"description mentions {', '.join(skill_hits[:6])}")
        scores[cid], reasons[cid] = s, r
    # Specific categories beat general SWE when both match the title.
    if scores.get("general_swe") and any(scores[c] >= 2 for c in scores if c != "general_swe"):
        scores["general_swe"] -= 1.0
    best = max(scores, key=lambda c: scores[c]) if scores else None
    if not best or scores[best] <= 0:
        return {"id": None, "label": None, "confidence": 0.0, "reasons": ["no category keywords matched"],
                "scores": scores}
    total = sum(v for v in scores.values() if v > 0)
    conf = round(min(1.0, scores[best] / 3.0) * (scores[best] / total), 2)
    return {"id": best, "label": categories[best].get("label", best), "confidence": conf,
            "reasons": reasons[best], "scores": {k: round(v, 2) for k, v in scores.items()}}


def requirement_groups(mandatory: list[str], alternatives: list[list[str]]) -> list[list[str]]:
    """Collapse required skills into requirements; alternatives count as one requirement."""
    groups: list[list[str]] = []
    used: set[str] = set()
    for s in mandatory:
        if s in used:
            continue
        group = next((g for g in alternatives if s in g), None)
        members = [m for m in (group or [s]) if m in mandatory] or [s]
        used.update(members)
        groups.append(members)
    return groups


def _location_check(job: Job, parsed: job_parser.ParsedRequirements, profile) -> dict:
    remote = job.remote_type
    accept = [normalize(x) for x in (profile.acceptable_locations or []) if x]
    if profile.current_location:
        accept.append(normalize(profile.current_location))
    jl = normalize(job.location)
    if parsed.location_constraints:
        cons = "; ".join(parsed.location_constraints)
        if accept and any(a and a.split()[0] in normalize(cons) for a in accept):
            return {"name": "location", "status": "pass", "explanation": f"posting requires: {cons}; matches your locations"}
        return {"name": "location", "status": "unknown" if not accept else "warn",
                "explanation": f"posting states: {cons} — verify you qualify"}
    if remote == "remote":
        return {"name": "location", "status": "pass", "explanation": "remote role (check any country restrictions)"}
    if not jl:
        return {"name": "location", "status": "unknown", "explanation": "job location not stated"}
    if not accept:
        return {"name": "location", "status": "unknown",
                "explanation": "your current/acceptable locations are not set in the profile"}
    if any(a and (a in jl or jl in a or a.split(",")[0].strip() in jl) for a in accept):
        return {"name": "location", "status": "pass", "explanation": f"'{job.location}' matches your locations"}
    if profile.willing_to_relocate is True:
        return {"name": "location", "status": "pass",
                "explanation": f"'{job.location}' is outside your listed locations, but you are willing to relocate"}
    if profile.willing_to_relocate is None:
        return {"name": "location", "status": "unknown",
                "explanation": f"'{job.location}' is outside your listed locations; relocation preference not set"}
    return {"name": "location", "status": "fail",
            "explanation": f"'{job.location}' is outside your listed locations and you prefer not to relocate"}


def _work_auth_check(parsed: job_parser.ParsedRequirements, profile) -> dict:
    if parsed.sponsorship == "not_available":
        if profile.needs_sponsorship is True:
            return {"name": "work authorization", "status": "fail",
                    "explanation": "posting says no visa sponsorship; your profile says you need sponsorship"}
        if profile.needs_sponsorship is None:
            return {"name": "work authorization", "status": "unknown",
                    "explanation": "posting says no visa sponsorship; your sponsorship need is not set"}
        return {"name": "work authorization", "status": "pass",
                "explanation": "no sponsorship offered; you do not need sponsorship"}
    if parsed.work_authorization_required:
        if profile.needs_sponsorship is None and not profile.work_authorization:
            return {"name": "work authorization", "status": "unknown",
                    "explanation": "posting requires work authorization; yours is not recorded"}
        if profile.needs_sponsorship:
            return {"name": "work authorization", "status": "warn",
                    "explanation": "posting requires existing work authorization; you need sponsorship"}
        return {"name": "work authorization", "status": "pass", "explanation": "work authorization requirement noted"}
    return {"name": "work authorization", "status": "not_stated",
            "explanation": "posting does not state sponsorship or authorization requirements"}


def _education_check(session: Session, parsed: job_parser.ParsedRequirements) -> dict | None:
    if not parsed.degree_required:
        return None
    confirmed = list(session.scalars(select(Education).where(Education.status == "confirmed")))
    if not confirmed:
        return {"name": "education", "status": "unknown",
                "explanation": f"posting mentions '{parsed.degree_required}'; no confirmed education in profile"}
    degrees = ", ".join(filter(None, (f"{e.degree or ''} {e.field_of_study or ''}".strip() for e in confirmed)))
    return {"name": "education", "status": "warn",
            "explanation": f"posting mentions '{parsed.degree_required}'; you have: {degrees or 'confirmed education'} "
                           "— verify it satisfies the requirement"}


def evaluate(session: Session, job: Job) -> dict[str, Any]:
    prefs = settings_store.get(session, "search_preferences")
    mcfg = settings_store.get(session, "matching")
    categories = settings_store.get(session, "role_categories")
    profile = profile_svc.get_profile(session)
    have = profile_svc.confirmed_skills(session)
    learning = profile_svc.learning_skills(session)
    parsed = job_parser.parse(job.title, job.description_text, job.location)

    category = classify(job.title, job.description_text, categories)
    missing_info: list[str] = []
    components: list[dict] = []
    title_norm = f" {normalize(job.title)} "

    # --- Exclusions -------------------------------------------------------
    excluded = [k for k in prefs.get("exclude_keywords", []) if k.strip() and contains_term(title_norm, k)]

    # --- 1. Role fit (35) ---------------------------------------------------
    if category["id"]:
        title_based = any(r.startswith("title") for r in category["reasons"])
        # Title match: 21-35 points by confidence. Description-only match: at most 12.
        pts = 35 * (0.6 + 0.4 * category["confidence"]) if title_based else 12 * min(1.0, 2 * category["confidence"])
        components.append({"name": "role fit", "points": round(pts, 1), "max": 35,
                           "explanation": f"classified as {category['label']}: " + "; ".join(category["reasons"])})
    else:
        components.append({"name": "role fit", "points": 0, "max": 35,
                           "explanation": "title/description match none of your target role categories"})

    # --- 2. Skills (35) -----------------------------------------------------
    mand, pref = parsed.mandatory_skills, parsed.preferred_skills
    mand_reqs = requirement_groups(mand, parsed.alternatives)
    have_m = [s for s in mand if s in have]
    have_p = [s for s in pref if s in have]
    met_reqs = [g for g in mand_reqs if any(s in have for s in g)]
    miss_m = [" or ".join(g) for g in mand_reqs if not any(s in have for s in g)]
    miss_p = [s for s in pref if s not in have]
    if not mand and not pref:
        components.append({"name": "skills", "points": 0, "max": 35,
                           "explanation": "no recognisable skills found in the posting — review manually"})
        missing_info.append("required skills (none recognised in posting)")
    elif not have:
        components.append({"name": "skills", "points": 0, "max": 35,
                           "explanation": "your profile has no confirmed skills yet"})
        missing_info.append("confirmed skills in your profile")
    else:
        if mand and pref:
            pts = 27 * len(met_reqs) / len(mand_reqs) + 8 * len(have_p) / len(pref)
        elif mand:
            pts = 35 * len(met_reqs) / len(mand_reqs)
        else:
            pts = 35 * len(have_p) / len(pref)
        bits = []
        if mand:
            bits.append(f"{len(met_reqs)}/{len(mand_reqs)} required")
        if pref:
            bits.append(f"{len(have_p)}/{len(pref)} preferred")
        components.append({"name": "skills", "points": round(pts, 1), "max": 35,
                           "explanation": f"you have {' and '.join(bits)} skills"
                           + (f"; missing: {', '.join(miss_m)}" if miss_m else "")})

    # --- 3. Preferences (30) -------------------------------------------------
    levels = prefs.get("experience_levels") or []
    lvl = job.experience_level or parsed.experience_level
    if lvl is None:
        pts, why = 5, "experience level not stated (partial credit)"
        missing_info.append("experience level")
    elif not levels or lvl in levels:
        pts, why = 10, f"level '{lvl}' is in your preferences"
    else:
        pts, why = 0, f"level '{lvl}' is outside your preferences ({', '.join(levels)})"
    components.append({"name": "level", "points": pts, "max": 10, "explanation": why})

    remote_prefs = prefs.get("remote_preferences") or []
    pref_locs = [normalize(x) for x in prefs.get("locations") or [] if x]
    jl = normalize(job.location)
    if job.remote_type == "unknown":
        pts, why = 5, "remote/hybrid/onsite not stated (partial credit)"
        missing_info.append("work arrangement (remote/hybrid/onsite)")
    elif remote_prefs and job.remote_type not in remote_prefs:
        pts, why = 0, f"{job.remote_type} is not in your preferences"
    elif job.remote_type == "remote" or not pref_locs:
        pts, why = 10, f"{job.remote_type} matches your preferences"
    elif jl and any(p in jl for p in pref_locs):
        pts, why = 10, f"{job.remote_type} in a preferred location ({job.location})"
    elif not jl:
        pts, why = 5, "location not stated (partial credit)"
        missing_info.append("job location")
    else:
        pts, why = 2, f"{job.location} is not one of your preferred locations"
    components.append({"name": "location", "points": pts, "max": 10, "explanation": why})

    inc = [k for k in prefs.get("include_keywords", []) if k.strip()]
    text_norm = normalize(f"{job.title} {job.description_text or ''}")
    if not inc:
        pts, why = 5, "no include-keywords configured"
    else:
        hits = [k for k in inc if contains_term(text_norm, k)]
        pts = 5 if hits else 0
        why = f"mentions {', '.join(hits)}" if hits else "none of your include-keywords appear"
    components.append({"name": "keywords", "points": pts, "max": 5, "explanation": why})

    min_sal = prefs.get("min_salary")
    sal_max = job.salary_max or job.salary_min
    if not min_sal:
        pts, why = 5, "no minimum salary configured"
    elif sal_max is None:
        pts, why = 2.5, "salary not stated (partial credit)"
        missing_info.append("salary")
    elif prefs.get("salary_currency") and job.salary_currency and prefs["salary_currency"] != job.salary_currency:
        pts, why = 2.5, f"salary in {job.salary_currency}, preference in {prefs['salary_currency']} — not compared"
    elif sal_max >= min_sal:
        pts, why = 5, f"salary up to {sal_max:,} meets your minimum {min_sal:,}"
    else:
        pts, why = 0, f"salary up to {sal_max:,} is below your minimum {min_sal:,}"
    components.append({"name": "salary", "points": pts, "max": 5, "explanation": why})

    score = round(sum(c["points"] for c in components), 1)
    if excluded:
        score = 0.0

    # --- Eligibility --------------------------------------------------------
    checks: list[dict] = []
    cand_years, years_src = profile_svc.experience_years(session)
    if parsed.experience_min_years is None:
        checks.append({"name": "experience", "status": "unknown", "key": True,
                       "explanation": "years of experience required are not stated"})
    elif cand_years is None:
        checks.append({"name": "experience", "status": "unknown", "key": True,
                       "explanation": f"requires {parsed.experience_min_years:g}+ years; your experience is unknown ({years_src})"})
        missing_info.append("your total experience (profile)")
    elif cand_years >= parsed.experience_min_years:
        checks.append({"name": "experience", "status": "pass", "key": True,
                       "explanation": f"requires {parsed.experience_min_years:g}+ years; you have {cand_years:g} ({years_src})"})
    elif cand_years >= parsed.experience_min_years - 0.5:
        checks.append({"name": "experience", "status": "warn", "key": True,
                       "explanation": f"requires {parsed.experience_min_years:g}+ years; you have {cand_years:g} — slightly below"})
    else:
        checks.append({"name": "experience", "status": "fail", "key": True,
                       "explanation": f"requires {parsed.experience_min_years:g}+ years; you have {cand_years:g} ({years_src})"})

    if not mand:
        checks.append({"name": "required skills", "status": "unknown", "key": True,
                       "explanation": "no required skills recognised in the posting"})
    elif not have:
        checks.append({"name": "required skills", "status": "unknown", "key": True,
                       "explanation": "no confirmed skills in your profile to compare"})
    else:
        ratio = len(miss_m) / len(mand_reqs)
        if ratio == 0:
            st, why = "pass", "you have all recognised required skills"
        elif ratio <= mcfg["max_missing_mandatory_ratio"]:
            st, why = "warn", f"missing {len(miss_m)}/{len(mand_reqs)} required: {', '.join(miss_m)}"
        else:
            st, why = "fail", f"missing {len(miss_m)}/{len(mand_reqs)} required: {', '.join(miss_m)}"
        checks.append({"name": "required skills", "status": st, "key": True, "explanation": why})

    checks.append({**_location_check(job, parsed, profile), "key": False})
    checks.append({**_work_auth_check(parsed, profile), "key": False})
    edu = _education_check(session, parsed)
    if edu:
        checks.append({**edu, "key": False})

    statuses = [c["status"] for c in checks]
    key_statuses = [c["status"] for c in checks if c["key"]]
    if "fail" in statuses:
        eligibility = "ineligible"
    elif "unknown" in key_statuses:
        eligibility = "unknown"
    elif "unknown" in statuses or "warn" in statuses:
        eligibility = "likely_eligible"
    else:
        eligibility = "eligible"

    review_reasons: list[str] = []
    threshold, band_low = mcfg["relevance_threshold"], mcfg["review_band_low"]
    if not excluded and job.status == "active":
        if score >= threshold and eligibility == "unknown":
            review_reasons.append("relevant, but eligibility could not be determined")
        if band_low <= score < threshold:
            review_reasons.append(f"borderline relevance ({score:g}, threshold {threshold})")
        if (job.raw or {}).get("partial"):
            review_reasons.append("job details were extracted without structured data")
        if score >= threshold and eligibility == "likely_eligible" and any(c["status"] == "warn" for c in checks):
            review_reasons.append("relevant, with eligibility warnings to check")

    return {
        "category": category,
        "relevance": {"score": score, "threshold": threshold, "relevant": score >= threshold and not excluded,
                      "components": components, "excluded_by": excluded or None},
        "eligibility": {"status": eligibility, "checks": checks},
        "skills": {"mandatory": mand, "preferred": pref, "have": sorted(set(have_m + have_p)),
                   "missing_mandatory": miss_m, "missing_preferred": miss_p,
                   "learning": sorted(s for s in mand + pref if s in learning and s not in have),
                   "all": parsed.all_skills, "alternatives": parsed.alternatives},
        "requirements": {"experience_min_years": parsed.experience_min_years,
                         "experience_max_years": parsed.experience_max_years,
                         "experience_evidence": parsed.experience_evidence,
                         "sponsorship": parsed.sponsorship, "degree_required": parsed.degree_required,
                         "location_constraints": parsed.location_constraints,
                         "relocation_offered": parsed.relocation_offered, "emails": parsed.emails},
        "missing_info": sorted(set(missing_info)),
        "needs_review": bool(review_reasons),
        "review_reasons": review_reasons,
    }


def match_job(session: Session, job: Job) -> dict[str, Any]:
    result = evaluate(session, job)
    job.category = result["category"]["id"]
    job.category_confidence = result["category"]["confidence"]
    job.relevance_score = result["relevance"]["score"]
    job.eligibility = result["eligibility"]["status"]
    job.match_details = result
    job.matched_at = utcnow()
    return result


def match_pending(session: Session) -> int:
    n = 0
    for job in session.scalars(select(Job).where(Job.matched_at.is_(None), Job.status == "active")):
        match_job(session, job)
        n += 1
    return n


def rematch_all(session: Session) -> int:
    n = 0
    for job in session.scalars(select(Job).where(Job.status == "active")):
        match_job(session, job)
        n += 1
    return n
