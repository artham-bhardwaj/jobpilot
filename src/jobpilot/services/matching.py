"""Job classification, relevance, eligibility and data confidence.

Three independent results are produced for each job, all from deterministic rules:

* **Relevance (0-100)**: how well the job fits what you *want*. Seven weighted components (weights
  configurable, normalised to 100): required skills, preferred skills, role alignment, experience level,
  education, location/work mode, other preferences (keywords, salary). Each has an explanation and evidence.
* **Eligibility**: whether you meet the *stated* requirements, using **confirmed** profile facts only.
    - ``eligible`` / ``likely_eligible`` (stated requirements met; some secondary information unknown)
    - ``unknown`` (a key requirement or your corresponding fact is missing; never treated as satisfied)
    - ``ineligible`` (a stated requirement is clearly not met)
  Failed checks are marked **hard** (configurable: large experience gap, sponsorship, location, excluded
  title keywords) or soft (e.g. skill gaps you could close). A hard conflict makes the recommendation
  "not suitable" however high the relevance score.
* **Data confidence** (high / medium / low): how much the posting actually told us.
"""

from __future__ import annotations

import re
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
        if skill_hits and cat.get("title_required") and not title_hits:
            skill_hits = []  # e.g. SDE I: a level only the title can state
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
        from jobpilot.services import regions

        if regions.remote_elsewhere(job):
            return {"name": "location", "status": "warn",
                    "explanation": f"remote, but only for candidates in '{job.location}' (not India or the UAE)"}
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
            "explanation": f"'{job.location}' is outside your listed locations and you prefer not to relocate",
            "rule": "location_conflict"}


def _work_auth_check(parsed: job_parser.ParsedRequirements, profile) -> dict:
    if parsed.sponsorship == "not_available":
        if profile.needs_sponsorship is True:
            return {"name": "work authorization", "status": "fail",
                    "explanation": "posting says no visa sponsorship; your profile says you need sponsorship",
                    "rule": "sponsorship_conflict"}
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
    if parsed.degree_equivalent_experience_ok:
        return {"name": "education", "status": "pass" if session.scalar(
            select(Education.id).where(Education.status == "confirmed").limit(1)) else "warn",
                "explanation": f"posting asks for '{parsed.degree_required}' and accepts equivalent experience"}
    confirmed = list(session.scalars(select(Education).where(Education.status == "confirmed")))
    if not confirmed:
        return {"name": "education", "status": "unknown",
                "explanation": f"posting mentions '{parsed.degree_required}'; no confirmed education in profile"}
    degrees = ", ".join(filter(None, (f"{e.degree or ''} {e.field_of_study or ''}".strip() for e in confirmed)))
    return {"name": "education", "status": "warn",
            "explanation": f"posting mentions '{parsed.degree_required}'; you have: {degrees or 'confirmed education'} "
                           "— verify it satisfies the requirement"}


COMPONENT_LABELS = {
    "required_skills": "required skills", "preferred_skills": "preferred skills", "role_alignment": "role alignment",
    "experience_level": "experience level", "education": "education", "location": "location / work mode",
    "preferences": "other preferences",
}
RECOMMENDATIONS = {
    "not_suitable": "Not suitable — hard conflict",
    "stretch": "Stretch — requirements not met",
    "strong": "Strong match",
    "relevant": "Relevant",
    "borderline": "Borderline",
    "low": "Low relevance",
}


def _structured_from_job(job: Job) -> dict[str, Any]:
    keys = set((job.raw or {}).get("_structured") or [])
    values = {"remote_type": job.remote_type, "employment_type": job.employment_type, "salary_min": job.salary_min,
              "salary_max": job.salary_max, "salary_currency": job.salary_currency,
              "valid_through": job.valid_through.isoformat() if job.valid_through else None, "location": job.location,
              "deadline_source": (job.raw or {}).get("deadline_source") if job.valid_through else None}
    return {k: v for k, v in values.items() if k in keys and v not in (None, "")}


_TEST_ROLE = re.compile(r"\b(qa|quality assurance|test(?:ing|er)?|sdet|automation engineer|verification)\b", re.I)
_DEV_ROLE = re.compile(r"\b(software|backend|back-end|frontend|front-end|full[- ]?stack|developer|programmer|sde|swe|"
                       r"engineer)\b", re.I)


def is_development_role(title: str | None) -> bool:
    """A software-development posting (not a testing/QA one), judged from the title only."""
    t = title or ""
    return bool(_DEV_ROLE.search(t)) and not _TEST_ROLE.search(t)


def _candidate_skill_note(name: str, info: dict | None) -> str:
    if not info:
        return "not in your profile"
    if info["status"] == "unverified":
        return "in your profile but UNVERIFIED (confirm it if accurate)"
    if info["status"] == "learning":
        return "you are learning it (not claimed)"
    where = []
    if info["work"]:
        where.append("work: " + "; ".join(info["work"][:2]))
    if info["projects"]:
        where.append("personal projects: " + "; ".join(info["projects"][:2]))
    if info.get("non_work"):
        where.append("academic/learning (not work): " + "; ".join(info["non_work"][:2]))
    return "confirmed" + (f" ({' | '.join(where)})" if where else " (no linked role or project)")


def evaluate(session: Session, job: Job, weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Score one job. ``weights`` overrides the saved weights (used for previews; nothing is stored)."""
    prefs = settings_store.get(session, "search_preferences")
    mcfg = settings_store.get(session, "matching")
    hard_cfg = settings_store.get(session, "hard_rules")
    rules = settings_store.get(session, "review_rules")
    categories = settings_store.get(session, "role_categories")
    from jobpilot.services import skills_taxonomy

    skills_taxonomy.configure_from_settings(session)
    W = {k: float(v) for k, v in (weights or settings_store.get(session, "matching_weights")).items()}
    scale = 100.0 / (sum(W.values()) or 1.0)
    profile = profile_svc.get_profile(session)
    cand = profile_svc.skill_profile(session)
    have = {k for k, v in cand.items() if v["status"] == "confirmed"}
    parsed = job_parser.parse(job.title, job.description_text, job.location, structured=_structured_from_job(job),
                              company=job.company_name)
    category = classify(job.title, job.description_text, categories)
    title_norm = f" {normalize(job.title)} "
    missing_job: list[str] = []
    missing_profile: list[str] = []
    components: list[dict] = []

    def component(key: str, fraction: float, explanation: str, evidence: list[str] | None = None) -> None:
        mx = W.get(key, 0) * scale
        components.append({"key": key, "name": COMPONENT_LABELS[key], "points": round(mx * max(0.0, min(1.0, fraction)), 1),
                           "max": round(mx, 1), "fraction": round(fraction, 2), "explanation": explanation,
                           "evidence": evidence or []})

    excluded = [k for k in prefs.get("exclude_keywords", []) if k.strip() and contains_term(title_norm, k)]

    # --- Role alignment ---------------------------------------------------------------------
    if category["id"]:
        title_based = any(r.startswith("title") for r in category["reasons"])
        frac = (0.6 + 0.4 * category["confidence"]) if title_based else 0.35 * min(1.0, 2 * category["confidence"])
        component("role_alignment", frac, f"classified as {category['label']}", category["reasons"])
    else:
        component("role_alignment", 0, "title/description match none of your target role categories")

    # --- Skills ----------------------------------------------------------------------------
    mand, pref = parsed.mandatory_skills, parsed.preferred_skills
    groups = requirement_groups(mand, parsed.alternatives)
    met = [g for g in groups if any(x in have for x in g)]
    missing_groups = [g for g in groups if not any(x in have for x in g)]
    unverified_hits = sorted({x for g in missing_groups for x in g if cand.get(x, {}).get("status") == "unverified"})
    req_evidence = []
    for g in groups:
        label = " or ".join(g)
        ev = parsed.skill_evidence.get(g[0], {})
        src = f"{ev.get('where', '?')}" + (f": “{ev['snippet'][:80]}”" if ev.get("snippet") else "")
        mine = next((x for x in g if x in have), None)
        note = _candidate_skill_note(mine, cand.get(mine)) if mine else "; ".join(
            f"{x}: {_candidate_skill_note(x, cand.get(x))}" for x in g)
        req_evidence.append(f"{label} — required ({ev.get('basis', '?')}, {src}) → {note}")
    if not groups:
        component("required_skills", 0, "no required skills recognised in the posting — no credit (data confidence is "
                                        "lowered; review manually if the role fits)")
        missing_job.append("required skills (none recognised in the posting)")
    elif not have:
        component("required_skills", 0, "your profile has no confirmed skills yet", req_evidence)
        missing_profile.append("confirmed skills")
    else:
        component("required_skills", len(met) / len(groups),
                  f"you have {len(met)}/{len(groups)} required" + (f"; missing: {', '.join(' or '.join(g) for g in missing_groups)}"
                                                                    if missing_groups else ""), req_evidence)
    have_p = [x for x in pref if x in have]
    if not pref:
        component("preferred_skills", 1.0 if groups else 0.5,
                  "no preferred skills listed" + ("" if groups else " (nor required ones — partial credit)"))
    else:
        component("preferred_skills", len(have_p) / len(pref) if have else 0,
                  f"you have {len(have_p)}/{len(pref)} preferred",
                  [f"{x} — preferred → {_candidate_skill_note(x, cand.get(x))}" for x in pref])

    # --- Experience level --------------------------------------------------------------------
    cand_years, years_src = profile_svc.experience_years(session)
    levels = prefs.get("experience_levels") or []
    lvl = job.experience_level or parsed.experience_level
    parts, ev = [], [f"seniority: {lvl or 'not stated'} ({parsed.evidence['seniority']['basis']})"]
    if lvl is None:
        parts.append(0.5)
        missing_job.append("seniority / experience level")
    else:
        parts.append(1.0 if (not levels or lvl in levels) else 0.0)
    req_min = parsed.experience_min_years
    if req_min is not None and cand_years is not None:
        gap = req_min - cand_years
        parts.append(1.0 if gap <= 0 else (0.5 if gap <= hard_cfg["experience_gap_years"] else 0.0))
        ev.append(f"requires {req_min:g}+ years (“{parsed.experience_evidence}”); you have {cand_years:g} ({years_src})")
    elif req_min is not None:
        ev.append(f"requires {req_min:g}+ years; your experience is unknown ({years_src})")
    component("experience_level", sum(parts) / len(parts),
              f"level '{lvl or 'unknown'}'" + (" is in your preferences" if lvl and (not levels or lvl in levels) else
                                               " is outside your preferences" if lvl else " (partial credit)"), ev)

    # --- Education ---------------------------------------------------------------------------
    confirmed_edu = list(session.scalars(select(Education).where(Education.status == "confirmed")))
    deg_ev = parsed.evidence.get("degree", {})
    if not parsed.degree_required:
        component("education", 1.0, "no degree requirement stated" if deg_ev.get("basis") == "unknown"
                  else f"degree {deg_ev.get('value')}", [deg_ev["snippet"]] if deg_ev.get("snippet") else [])
    elif confirmed_edu:
        component("education", 1.0, f"posting asks for '{parsed.degree_required}'; you have confirmed education — "
                                    "verify the field/level matches", [e.degree or e.institution for e in confirmed_edu])
    elif parsed.degree_equivalent_experience_ok:
        component("education", 0.5, "degree or equivalent experience accepted; no confirmed education in your profile")
        missing_profile.append("education (confirmed)")
    else:
        component("education", 0, f"posting asks for '{parsed.degree_required}'; no confirmed education in your profile")
        missing_profile.append("education (confirmed)")

    # --- Location / work mode ------------------------------------------------------------------
    remote_prefs = prefs.get("remote_preferences") or []
    pref_locs = [normalize(x) for x in prefs.get("locations") or [] if x]
    jl = normalize(job.location)
    wm = parsed.evidence.get("work_mode", {})
    loc_ev = [f"work mode: {job.remote_type} ({wm.get('basis', 'unknown')})", f"location: {job.location or 'not stated'}"]
    any_mode = not remote_prefs or {"remote", "hybrid", "onsite"} <= set(remote_prefs)
    if job.remote_type == "unknown" and any_mode:
        # Every work mode is acceptable to you, so the unstated mode can't make the job less suitable: score the place.
        missing_job.append("work arrangement (remote/hybrid/onsite)")
        if not pref_locs:
            frac, why = 1.0, "work mode not stated; any work mode suits your preferences"
        elif jl and any(p in jl for p in pref_locs):
            frac, why = 1.0, f"work mode not stated; in a preferred location ({job.location})"
        elif not jl:
            frac, why = 0.5, "work mode and location not stated (partial credit)"
        else:
            frac, why = 0.2, f"work mode not stated; {job.location} is not one of your preferred locations"
    elif job.remote_type == "unknown":
        frac, why = 0.5, "work mode not stated (partial credit)"
        missing_job.append("work arrangement (remote/hybrid/onsite)")
    elif remote_prefs and job.remote_type not in remote_prefs:
        frac, why = 0.0, f"{job.remote_type} is not in your preferences"
    elif job.remote_type == "remote" or not pref_locs:
        frac, why = 1.0, f"{job.remote_type} matches your preferences"
    elif jl and any(p in jl for p in pref_locs):
        frac, why = 1.0, f"{job.remote_type} in a preferred location ({job.location})"
    elif not jl:
        frac, why = 0.5, "location not stated (partial credit)"
        missing_job.append("job location")
    else:
        frac, why = 0.2, f"{job.location} is not one of your preferred locations"
    component("location", frac, why, loc_ev)

    # --- Other preferences ---------------------------------------------------------------------
    inc = [k for k in prefs.get("include_keywords", []) if k.strip()]
    text_norm = normalize(f"{job.title} {job.description_text or ''}")
    hits = [k for k in inc if contains_term(text_norm, k)]
    kw = 1.0 if not inc else (1.0 if hits else 0.0)
    kw_why = "no include-keywords configured" if not inc else (f"mentions {', '.join(hits)}" if hits else "none of your include-keywords appear")
    min_sal = prefs.get("min_salary")
    sal_max = job.salary_max or job.salary_min
    if not min_sal:
        sal, sal_why = 1.0, "no minimum salary configured"
    elif sal_max is None:
        sal, sal_why = 0.5, "salary not stated (partial credit)"
        missing_job.append("salary")
    elif prefs.get("salary_currency") and job.salary_currency and prefs["salary_currency"] != job.salary_currency:
        sal, sal_why = 0.5, f"salary in {job.salary_currency}, preference in {prefs['salary_currency']} — not compared"
    elif sal_max >= min_sal:
        sal, sal_why = 1.0, f"salary up to {sal_max:,} meets your minimum {min_sal:,}"
    else:
        sal, sal_why = 0.0, f"salary up to {sal_max:,} is below your minimum {min_sal:,}"
    component("preferences", 0.6 * kw + 0.4 * sal, f"{kw_why}; {sal_why}")

    score = round(sum(c["points"] for c in components), 1)
    if excluded:
        score = 0.0

    # --- Eligibility ---------------------------------------------------------------------------
    checks: list[dict] = []
    gap_limit = hard_cfg["experience_gap_years"]
    breakdown = profile_svc.experience_breakdown(session)
    dev_years = breakdown["development"]
    dev_role = is_development_role(job.title)
    if req_min is None:
        checks.append({"name": "experience", "status": "unknown", "key": True,
                       "explanation": "years of experience required are not stated"})
    elif cand_years is None:
        checks.append({"name": "experience", "status": "unknown", "key": True,
                       "explanation": f"requires {req_min:g}+ years; your work experience is unknown ({years_src})"})
        missing_profile.append("total work experience")
    elif cand_years >= req_min and dev_role and dev_years < req_min:
        # Enough years overall, but not in development roles: never present testing/other work as development.
        split = ", ".join(f"{v:g} {k.replace('_', ' ')}" for k, v in breakdown["by_type"].items())
        note = (f"; kind of work not recorded for {breakdown['not_recorded']} role(s) — set it on your profile"
                if breakdown["not_recorded"] else "")
        checks.append({"name": "experience", "status": "warn", "key": True,
                       "explanation": f"requires {req_min:g}+ years for a development role; you have {cand_years:g} years "
                                      f"of work experience, of which {dev_years:g} in software development roles "
                                      f"({split}){note}. Check whether your experience fits before applying."})
        if breakdown["not_recorded"]:
            missing_profile.append("kind of work for each role (development / testing / other)")
    elif cand_years >= req_min:
        checks.append({"name": "experience", "status": "pass", "key": True,
                       "explanation": f"requires {req_min:g}+ years; you have {cand_years:g} years of work experience "
                                      f"({years_src}; personal projects not counted)"})
    elif req_min - cand_years <= 0.5:
        checks.append({"name": "experience", "status": "warn", "key": True,
                       "explanation": f"requires {req_min:g}+ years; you have {cand_years:g} — slightly below"})
    else:
        gap = req_min - cand_years
        checks.append({"name": "experience", "status": "fail", "key": True, "rule": "experience_gap_years" if gap > gap_limit else None,
                       "explanation": f"requires {req_min:g}+ years; you have {cand_years:g} ({years_src}; personal "
                                      f"projects not counted) — {gap:g} year(s) short"})
    if not groups:
        checks.append({"name": "required skills", "status": "unknown", "key": True,
                       "explanation": "no required skills recognised in the posting"})
    elif not have:
        checks.append({"name": "required skills", "status": "unknown", "key": True,
                       "explanation": "no confirmed skills in your profile to compare"})
    else:
        ratio = len(missing_groups) / len(groups)
        names = ", ".join(" or ".join(g) for g in missing_groups)
        extra = f" ({', '.join(unverified_hits)} listed in your profile but unverified)" if unverified_hits else ""
        if ratio == 0:
            st, why = "pass", "you have all recognised required skills (confirmed)"
        elif ratio <= mcfg["max_missing_mandatory_ratio"]:
            st, why = "warn", f"missing {len(missing_groups)}/{len(groups)} required: {names}{extra}"
        else:
            st, why = "fail", f"missing {len(missing_groups)}/{len(groups)} required: {names}{extra}"
        checks.append({"name": "required skills", "status": st, "key": True, "explanation": why})
    checks.append({**_location_check(job, parsed, profile), "key": False})
    checks.append({**_work_auth_check(parsed, profile), "key": False})
    edu = _education_check(session, parsed)
    if edu:
        checks.append({**edu, "key": False})
    for c in checks:
        rule = c.pop("rule", None)
        c["hard"] = bool(c["status"] == "fail" and rule and hard_cfg.get(rule))
    if excluded and hard_cfg["excluded_title_keywords"]:
        checks.append({"name": "title keywords", "status": "fail", "key": False, "hard": True,
                       "explanation": f"title contains excluded keyword(s): {', '.join(excluded)}"})
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
    hard_conflicts = [c["explanation"] for c in checks if c["hard"]]
    if profile.acceptable_locations is None and not profile.current_location:
        missing_profile.append("your locations")
    if profile.needs_sponsorship is None and parsed.sponsorship != "not_stated":
        missing_profile.append("whether you need visa sponsorship")

    # --- Data confidence -------------------------------------------------------------------------
    raw = job.raw or {}
    signals = [
        ("description is substantial (≥ 400 characters)", len(job.description_text or "") >= 400),
        ("requirements section found", parsed.requirements_section_found),
        ("at least 2 required skills recognised", len(mand) >= 2),
        ("experience requirement stated", req_min is not None),
        ("location or work mode stated", bool(job.location) or job.remote_type != "unknown"),
        ("structured data from the source", bool(raw.get("_structured"))),
        ("not a partial page extraction", not raw.get("partial")),
    ]
    n_ok = sum(ok for _, ok in signals)
    confidence = "high" if n_ok >= 6 else ("medium" if n_ok >= 4 else "low")

    # --- Recommendation and review rules -----------------------------------------------------------
    threshold, strong, band_low = mcfg["relevance_threshold"], mcfg.get("strong_threshold", 75), mcfg["review_band_low"]
    if hard_conflicts:
        rec = "not_suitable"
    elif eligibility == "ineligible":
        rec = "stretch"
    elif score >= strong and eligibility in ("eligible", "likely_eligible"):
        rec = "strong"
    elif score >= threshold:
        rec = "relevant"
    elif score >= band_low:
        rec = "borderline"
    else:
        rec = "low"
    review_reasons: list[str] = []
    if job.status == "active" and rec != "not_suitable":
        if rules.get("strong_match") and rec == "strong":
            review_reasons.append("strong match — decide whether to apply")
        if rules.get("relevant_but_eligibility_unknown") and score >= threshold and eligibility == "unknown":
            review_reasons.append("relevant, but eligibility could not be determined")
        if rules.get("borderline_relevance") and band_low <= score < threshold:
            review_reasons.append(f"borderline relevance ({score:g}, threshold {threshold})")
        if rules.get("partial_extraction") and raw.get("partial"):
            review_reasons.append("job details were extracted without structured data")
        if rules.get("eligibility_warnings") and score >= threshold and eligibility == "likely_eligible" \
                and any(c["status"] == "warn" for c in checks):
            review_reasons.append("relevant, with eligibility warnings to check")
        if rules.get("low_confidence_relevant") and score >= threshold and confidence == "low":
            review_reasons.append("relevant, but the posting gave little information (low data confidence)")

    skills_block = {
        "mandatory": mand, "preferred": pref, "mentioned": parsed.mentioned_skills,
        "have": sorted({x for x in mand + pref if x in have}),
        "unverified": sorted({x for x in mand + pref if cand.get(x, {}).get("status") == "unverified"}),
        "learning": sorted({x for x in mand + pref if cand.get(x, {}).get("status") == "learning"}),
        "missing_mandatory": [" or ".join(g) for g in missing_groups],
        "missing_preferred": [x for x in pref if x not in have],
        "alternatives": parsed.alternatives, "all": parsed.all_skills,
        "job_evidence": parsed.skill_evidence,
        "candidate": {x: _candidate_skill_note(x, cand.get(x)) for x in mand + pref},
    }
    return {
        "category": category,
        "relevance": {"score": score, "threshold": threshold, "strong_threshold": strong,
                      "relevant": score >= threshold and not excluded and not hard_conflicts,
                      "components": components, "excluded_by": excluded or None,
                      "weights": {k: round(v * scale, 1) for k, v in W.items()}},
        "eligibility": {"status": eligibility, "checks": checks, "hard_conflicts": hard_conflicts},
        "confidence": {"level": confidence, "signals": [{"name": n, "ok": ok} for n, ok in signals]},
        "recommendation": {"key": rec, "label": RECOMMENDATIONS[rec]},
        "skills": skills_block,
        "requirements": {"experience_min_years": req_min, "experience_max_years": parsed.experience_max_years,
                         "experience_evidence": parsed.experience_evidence, "sponsorship": parsed.sponsorship,
                         "degree_required": parsed.degree_required,
                         "degree_equivalent_ok": parsed.degree_equivalent_experience_ok,
                         "employment_type": parsed.employment_type, "deadline": parsed.application_deadline,
                         "location_constraints": parsed.location_constraints,
                         "relocation_offered": parsed.relocation_offered, "emails": parsed.emails,
                         "evidence": parsed.evidence},
        "missing_info": sorted(set(missing_job + missing_profile)),
        "missing_job_info": sorted(set(missing_job)),
        "missing_profile_info": sorted(set(missing_profile)),
        "needs_review": bool(review_reasons),
        "review_reasons": review_reasons,
    }


def match_job(session: Session, job: Job) -> dict[str, Any]:
    result = evaluate(session, job)
    job.category = result["category"]["id"]
    job.category_confidence = result["category"]["confidence"]
    job.relevance_score = result["relevance"]["score"]
    job.eligibility = result["eligibility"]["status"]
    job.confidence = result["confidence"]["level"]
    job.hard_conflict = bool(result["eligibility"]["hard_conflicts"])
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


# Actions that change what a job's score depends on: your profile, skills, preferences and matching settings.
_SCORE_INPUT_ACTIONS = ("profile.%", "fact.%", "learning.%", "settings.%")


def inputs_changed_at(session: Session):
    """When anything a score depends on last changed (from the audit log), or None."""
    from sqlalchemy import func, or_

    from jobpilot.models import AuditLog

    return session.scalar(select(func.max(AuditLog.ts)).where(or_(*[AuditLog.action.like(a) for a in _SCORE_INPUT_ACTIONS])))


def rematch_stale(session: Session, limit: int = 2000) -> int:
    """Re-score active jobs that are unscored or were scored before your profile/settings last changed.

    Unchanged jobs keep their score, so ranking stays stable between runs. Bounded per run.
    """
    changed = inputs_changed_at(session)
    cond = Job.matched_at.is_(None)
    if changed is not None:
        cond = cond | (Job.matched_at < changed)
    n = 0
    for job in session.scalars(select(Job).where(Job.status == "active", cond).order_by(Job.id).limit(limit)):
        match_job(session, job)
        n += 1
    return n
