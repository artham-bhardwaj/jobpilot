"""Deterministic extraction of structured requirements from job text.

Everything returned here is *derived from the posting text*. When something
is not stated, the value is ``None`` and callers must treat it as unknown.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from jobpilot.services.skills_taxonomy import extract_skills
from jobpilot.services.text_utils import normalize

_PREFERRED_HEAD = re.compile(
    r"(preferred|nice[\s-]to[\s-]have|bonus|good[\s-]to[\s-]have|desired|pluses|a plus|additional qualifications|"
    r"preferred qualifications|what would be great|extra credit)", re.I)
_REQUIRED_HEAD = re.compile(
    r"(requirements?|required|qualifications|must[\s-]have|what you.ll need|what you need|you need|what we need|"
    r"you should have|ideal candidate|minimum requirements|key skills|your skills|your profile|tech(?:nical)? skills|"
    r"what we.re looking for|"
    r"who you are|you have|basic qualifications|minimum qualifications|skills|about you|you.ll bring|"
    r"what you bring|experience)", re.I)
_OTHER_HEAD = re.compile(
    r"(benefits|perks|what we offer|about us|about the company|our culture|compensation|equal opportunity|"
    r"responsibilities|what you.ll do|the role|about the role|day to day|why join)", re.I)

_YEARS = re.compile(
    r"(?P<min>\d{1,2}(?:\.\d)?)\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(?P<max>\d{1,2}(?:\.\d)?)\s*)?\+?\s*"
    r"(?:years?|yrs?)(?:'|’)?\s*(?:of\s+)?(?:[a-z/\-+#. ]{0,40}?)?\s*(?:experience|exp\b)",
    re.I,
)
_YEARS_MIN_WORDS = re.compile(r"(?:minimum|at least|min\.?)\s+(?:of\s+)?(?P<min>\d{1,2})\s*(?:years?|yrs?)", re.I)
_SPONSOR_NO = re.compile(
    r"(not\s+(?:able|able to)\s+(?:to\s+)?sponsor|no\s+(?:visa\s+)?sponsorship|unable to (?:provide\s+)?sponsor|"
    r"without (?:the need for )?(?:visa )?sponsorship|sponsorship (?:is )?not (?:available|provided|offered)|"
    r"will not sponsor|cannot sponsor|(?:do|does|will|can) not (?:offer|provide|support) (?:any )?(?:visa |work visa )?sponsorship)", re.I)
_AUTH_REQ = re.compile(r"(must be (?:legally )?authori[sz]ed to work|work authori[sz]ation (?:is )?required|"
                       r"eligible to work in|right to work in)", re.I)
_SPONSOR_YES = re.compile(r"(visa sponsorship (?:is )?available|we (?:will|can) sponsor|sponsorship provided)", re.I)
_LOCATED = re.compile(r"((?:must|should) (?:be )?(?:located|based|reside|live) in [^.\n;]{2,60})", re.I)
_RELOCATE = re.compile(r"(relocation (?:assistance|support|package) (?:is )?(?:available|provided|offered))", re.I)
_DEGREE = re.compile(r"\b(bachelor'?s?|master'?s?|b\.\s?tech|btech|b\.e\.|b\.s\.|b\.?sc|m\.\s?tech|mtech|m\.s\.|"
                     r"ph\.?d|(?:university |college )?degree)(?![a-z])\s*(?:degree\s+)?(?:in|of)?\s*([A-Za-z ,/&]{0,60})", re.I)
_REMOTE = re.compile(r"\b(fully remote|100% remote|remote[- ]first|remote(?: position| role| work)?|work from home|wfh)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\b(on[- ]?site|in[- ]office|office[- ]based|work from office|wfo)\b", re.I)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_SALARY_RANGE = re.compile(
    r"(?P<cur>[$€£₹]|usd|eur|gbp|inr|rs\.?)\s?(?P<a>\d[\d,]*(?:\.\d+)?)\s?(?P<ak>k|lpa|lakhs?|l)?\s*"
    r"(?:-|–|to)\s*(?:[$€£₹]|usd|eur|gbp|inr|rs\.?)?\s?(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?P<bk>k|lpa|lakhs?|l)?",
    re.I,
)
_LPA_RANGE = re.compile(r"(?P<a>\d{1,3}(?:\.\d+)?)\s*(?:-|–|to)\s*(?P<b>\d{1,3}(?:\.\d+)?)\s*(?:lpa|lakhs?(?: per annum)?)", re.I)
_CURRENCY = {"$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "₹": "INR",
             "inr": "INR", "rs": "INR", "rs.": "INR"}

SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|architect|head of|director|manager|vp|iii|iv|l5|l6)\b", re.I)
ENTRY_TITLE = re.compile(r"\b(junior|jr\.?|entry[- ]level|new grad|graduate|associate|intern|trainee|fresher|"
                         r"sde[- ]?(?:i|1)|engineer i|developer i|l3|level 1|early career)\b", re.I)


@dataclass
class ParsedRequirements:
    mandatory_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    # Skills named only incidentally ("our stack uses Kafka", benefits/about text): never requirements.
    mentioned_skills: list[str] = field(default_factory=list)
    all_skills: list[str] = field(default_factory=list)
    # skill -> {"kind": required|preferred|mentioned, "basis": explicit|inferred, "where": ..., "snippet": ...}
    skill_evidence: dict[str, dict] = field(default_factory=dict)
    requirements_section_found: bool = False
    experience_min_years: float | None = None
    experience_max_years: float | None = None
    experience_evidence: str | None = None
    experience_level: str | None = None
    remote_type: str = "unknown"
    employment_type: str | None = None
    application_deadline: str | None = None  # ISO date, when stated in the text
    sponsorship: str = "not_stated"  # not_available | available | not_stated
    work_authorization_required: bool = False
    location_constraints: list[str] = field(default_factory=list)
    relocation_offered: bool = False
    degree_required: str | None = None
    degree_equivalent_experience_ok: bool = False
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    emails: list[str] = field(default_factory=list)
    # Groups of interchangeable skills stated on one line ("Django or FastAPI").
    alternatives: list[list[str]] = field(default_factory=list)
    # field -> {"value": ..., "basis": explicit|inferred|unknown, "snippet": text it came from}
    evidence: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _is_heading(line: str) -> bool:
    s = line.strip().rstrip(":").strip()
    return 0 < len(s) <= 60 and not s.startswith("•") and (line.strip().endswith(":") or len(s.split()) <= 6)


def split_sections(text: str) -> tuple[str, str, str]:
    """Split text into (required, preferred, other) by heading lines and inline markers."""
    required, preferred, other = [], [], []
    current = other
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_heading(line):
            if _PREFERRED_HEAD.search(line):
                current = preferred
            elif _OTHER_HEAD.search(line):
                current = other
            elif _REQUIRED_HEAD.search(line):
                current = required
            # Keep the heading text too: short lines like "Strong Java skills" carry content.
            current.append(line)
            continue
        if current is not preferred and _PREFERRED_HEAD.search(line):
            preferred.append(line)  # e.g. "Experience with Kafka is a plus"
        else:
            current.append(line)
    return "\n".join(required), "\n".join(preferred), "\n".join(other)


def parse_experience(text: str) -> tuple[float | None, float | None, str | None]:
    best: tuple[float, float | None, str] | None = None
    for m in _YEARS.finditer(text):
        lo = float(m.group("min"))
        hi = float(m.group("max")) if m.group("max") else None
        if lo > 30 or (hi is not None and hi < lo):
            continue
        # Prefer the smallest stated minimum (the overall requirement is usually the lowest).
        if best is None or lo < best[0]:
            best = (lo, hi, m.group(0).strip())
    if best is None:
        m = _YEARS_MIN_WORDS.search(text)
        if m:
            best = (float(m.group("min")), None, m.group(0).strip())
    if best is None:
        return None, None, None
    return best


def level_from(title: str, exp_min: float | None) -> str | None:
    if SENIOR_TITLE.search(title or ""):
        return "senior"
    if ENTRY_TITLE.search(title or ""):
        return "entry"
    if exp_min is not None:
        if exp_min <= 2:
            return "entry"
        if exp_min <= 5:
            return "mid"
        return "senior"
    return None


def remote_from(*texts: str | None) -> str:
    joined = " ".join(t for t in texts if t)
    if _HYBRID.search(joined):
        return "hybrid"
    if _REMOTE.search(joined) and not re.search(r"\bnot\s+(?:a\s+)?remote\b|\bno remote\b", joined, re.I):
        return "remote"
    if _ONSITE.search(joined):
        return "onsite"
    return "unknown"


def _num(s: str, suffix: str | None) -> float:
    v = float(s.replace(",", ""))
    suffix = (suffix or "").lower()
    if suffix == "k":
        v *= 1000
    elif suffix in ("lpa", "l", "lakh", "lakhs"):
        v *= 100000
    return v


def parse_salary(text: str) -> tuple[int | None, int | None, str | None]:
    m = _SALARY_RANGE.search(text)
    if m:
        a = _num(m.group("a"), m.group("ak") or m.group("bk"))
        b = _num(m.group("b"), m.group("bk"))
        if a > b:
            a, b = b, a
        cur = _CURRENCY.get(m.group("cur").lower().rstrip("."), None) or _CURRENCY.get(m.group("cur").lower())
        if b >= 1000:
            return int(a), int(b), cur
    m = _LPA_RANGE.search(text)
    if m:
        return int(float(m.group("a")) * 100000), int(float(m.group("b")) * 100000), "INR"
    return None, None, None


_REQ_CUE = re.compile(r"\b(experience (?:with|in|using)|proficien\w*|knowledge of|expertise|must(?:[- ]have)?|required|"
                      r"requirements?|qualifications?|we(?:'re| are) looking for|you (?:have|bring|need)|"
                      r"strong|solid|hands[- ]on|background in|skilled|years? of|ability to|understanding of|"
                      r"working knowledge|competen\w*|fluency|fluent)\b", re.I)
_PREF_CUE = re.compile(r"\b(nice to have|good to have|a plus|is a plus|bonus|preferred|desirable|ideally|"
                       r"familiarity|exposure to|would be great)\b", re.I)
_RESP_CUE = re.compile(r"\b(you will|you'll|you will be|build|develop|design|implement|maintain|own|work on|write)\b", re.I)
_INCIDENTAL = re.compile(r"\b(our (?:tech )?stack|tech stack|we use|we're using|built (?:on|with)|powered by|our platform|"
                         r"our products?|our customers|we are a|about us|we offer|perks|benefits|"
                         r"equal opportunity)\b", re.I)
_EMPLOYMENT = [
    ("internship", re.compile(r"\b(internship|intern(?:s)?\b)", re.I)),
    ("contract", re.compile(r"\b(contract(?:or)?(?: role| position| basis)?|fixed[- ]term|\d+[- ]month contract)\b", re.I)),
    ("part_time", re.compile(r"\bpart[- ]time\b", re.I)),
    ("temporary", re.compile(r"\btemporary\b", re.I)),
    ("full_time", re.compile(r"\b(full[- ]time|permanent)\b", re.I)),
]
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DEADLINE = re.compile(
    r"(?:apply by|application deadline|deadline(?: to apply| for applications)?|applications? (?:close|closes|closing)(?: on)?|"
    r"closing date|last date(?: to apply)?)\s*[:\-]?\s*(?:is\s+)?"
    rf"(?P<date>\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})[a-z]*,?\s+\d{{4}}|(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})",
    re.I)
_DEGREE_EQUIV = re.compile(r"\bor equivalent(?: practical| professional| work| relevant)? experience\b", re.I)


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        out.extend(x.strip() for x in re.split(r"(?<=[.!?;])\s+", line) if x.strip())
    return out


def _parse_date(text: str) -> str | None:
    from datetime import datetime

    t = re.sub(r"(\d)(st|nd|rd|th)", r"\1", text.replace(",", " ")).strip()
    t = re.sub(r"\s+", " ", t)
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(t.replace("Sept", "Sep"), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _ev(value, basis: str, snippet: str | None = None) -> dict:
    return {"value": value, "basis": basis, "snippet": (snippet or "")[:200] or None}


def classify_skills(title: str, text: str, company: str | None = None) -> tuple[dict[str, dict], bool]:
    """Classify every recognised skill as required / preferred / mentioned, with where it came from.

    The employer's own name is ignored ("GitLab" in GitLab's postings is not a git requirement)."""
    ignore = [company] if company else None

    def skills_in(s: str) -> list[str]:
        return extract_skills(s, ignore=ignore)

    req_txt, pref_txt, other_txt = split_sections(text)
    has_req = bool(req_txt.strip())
    ev: dict[str, dict] = {}

    def put(skill: str, kind: str, basis: str, where: str, snippet: str) -> None:
        rank = {"required": 3, "preferred": 2, "mentioned": 1}
        if skill not in ev or rank[kind] > rank[ev[skill]["kind"]]:
            ev[skill] = {"kind": kind, "basis": basis, "where": where, "snippet": snippet[:200]}

    for sentence in _sentences(req_txt):
        for sk in skills_in(sentence):
            kind = "preferred" if _PREF_CUE.search(sentence) else "required"
            put(sk, kind, "explicit", "requirements section", sentence)
    for sentence in _sentences(pref_txt):
        for sk in skills_in(sentence):
            put(sk, "preferred", "explicit", "preferred / nice-to-have", sentence)
    for sentence in _sentences(other_txt):
        skills = skills_in(sentence)
        if not skills:
            continue
        if _INCIDENTAL.search(sentence):
            kind, basis, where = "mentioned", "explicit", "company/stack description (not a requirement)"
        elif _PREF_CUE.search(sentence):
            kind, basis, where = "preferred", "explicit", "preference wording"
        elif _REQ_CUE.search(sentence):
            kind, basis, where = "required", "explicit", "requirement wording"
        elif not has_req and _RESP_CUE.search(sentence):
            kind, basis, where = "required", "inferred", "responsibilities (no requirements section)"
        elif not has_req and re.match(r"^\s*[•\-*·]", sentence):
            kind, basis, where = "required", "inferred", "listed in the posting (no requirements section)"
        else:
            kind, basis, where = "mentioned", "explicit", "mentioned in passing"
        for sk in skills:
            put(sk, kind, basis, where, sentence)
    if not has_req:
        for sk in skills_in(title):
            put(sk, "required", "inferred", "job title", title)
    return ev, has_req


def parse(title: str, description: str | None, location: str | None = None,
          structured: dict | None = None, company: str | None = None) -> ParsedRequirements:
    """Extract requirements from a posting. ``structured`` holds values supplied by the source's structured
    data (e.g. ``{"remote_type": "remote", "employment_type": "Full-time"}``); those take precedence."""
    text = description or ""
    structured = structured or {}
    p = ParsedRequirements()

    p.skill_evidence, p.requirements_section_found = classify_skills(title, text, company)
    p.mandatory_skills = [k for k, v in p.skill_evidence.items() if v["kind"] == "required"]
    p.preferred_skills = [k for k, v in p.skill_evidence.items() if v["kind"] == "preferred"]
    p.mentioned_skills = [k for k, v in p.skill_evidence.items() if v["kind"] == "mentioned"]
    p.all_skills = list(p.skill_evidence)
    p.alternatives = [g for g in find_alternatives(text) if any(s in p.mandatory_skills for s in g)]

    p.experience_min_years, p.experience_max_years, p.experience_evidence = parse_experience(text)
    p.evidence["experience"] = (_ev(p.experience_min_years, "explicit", p.experience_evidence)
                                if p.experience_min_years is not None else _ev(None, "unknown"))

    p.experience_level = level_from(title, p.experience_min_years)
    if p.experience_level is None:
        p.evidence["seniority"] = _ev(None, "unknown")
    elif SENIOR_TITLE.search(title or "") or ENTRY_TITLE.search(title or ""):
        p.evidence["seniority"] = _ev(p.experience_level, "inferred", f"title: {title}")
    else:
        p.evidence["seniority"] = _ev(p.experience_level, "inferred", f"from experience: {p.experience_evidence}")

    if structured.get("remote_type"):
        p.remote_type = structured["remote_type"]
        p.evidence["work_mode"] = _ev(p.remote_type, "structured", "source data")
    else:
        p.remote_type = remote_from(title, location, text[:3000])
        p.evidence["work_mode"] = _ev(p.remote_type, "explicit", None) if p.remote_type != "unknown" else _ev(None, "unknown")

    if structured.get("employment_type"):
        p.employment_type = str(structured["employment_type"])
        p.evidence["employment_type"] = _ev(p.employment_type, "structured", "source data")
    else:
        for kind, rx in _EMPLOYMENT:
            m = rx.search(f"{title}\n{text}")
            if m:
                p.employment_type = kind
                p.evidence["employment_type"] = _ev(kind, "explicit", m.group(0))
                break
        else:
            p.evidence["employment_type"] = _ev(None, "unknown")

    if structured.get("valid_through"):
        p.application_deadline = str(structured["valid_through"])[:10]
        p.evidence["deadline"] = _ev(p.application_deadline, "structured", "source data (validThrough)")
    else:
        m = _DEADLINE.search(text)
        p.application_deadline = _parse_date(m.group("date")) if m else None
        p.evidence["deadline"] = _ev(p.application_deadline, "explicit", m.group(0)) if p.application_deadline else _ev(None, "unknown")

    m = _SPONSOR_NO.search(text) or _SPONSOR_YES.search(text)
    if _SPONSOR_NO.search(text):
        p.sponsorship = "not_available"
    elif _SPONSOR_YES.search(text):
        p.sponsorship = "available"
    p.evidence["sponsorship"] = _ev(p.sponsorship, "explicit", m.group(0)) if m else _ev("not_stated", "unknown")
    a = _AUTH_REQ.search(text)
    p.work_authorization_required = bool(a)
    p.location_constraints = list(dict.fromkeys(m.group(1).strip() for m in _LOCATED.finditer(text)))[:5]
    p.relocation_offered = bool(_RELOCATE.search(text))
    req_txt, pref_txt, other_txt = split_sections(text)
    d = _DEGREE.search(req_txt) or _DEGREE.search(other_txt)
    if d:
        p.degree_required = d.group(0).strip()[:120]
        p.degree_equivalent_experience_ok = bool(_DEGREE_EQUIV.search(text))
        p.evidence["degree"] = _ev(p.degree_required, "explicit", d.group(0))
    elif (d := _DEGREE.search(pref_txt)):
        p.evidence["degree"] = _ev(f"preferred, not required: {d.group(0).strip()[:100]}", "explicit", d.group(0))
    else:
        p.evidence["degree"] = _ev(None, "unknown")

    if structured.get("salary_min") or structured.get("salary_max"):
        p.salary_min, p.salary_max = structured.get("salary_min"), structured.get("salary_max")
        p.salary_currency = structured.get("salary_currency")
        p.evidence["salary"] = _ev([p.salary_min, p.salary_max, p.salary_currency], "structured", "source data")
    else:
        p.salary_min, p.salary_max, p.salary_currency = parse_salary(text)
        p.evidence["salary"] = (_ev([p.salary_min, p.salary_max, p.salary_currency], "explicit", None)
                                if p.salary_min else _ev(None, "unknown"))
    p.evidence["location"] = _ev(location, "structured" if structured.get("location") else "explicit") if location else _ev(None, "unknown")
    p.emails = sorted(set(_EMAIL.findall(text)))
    return p


_OR = re.compile(r"\b(?:or|either)\b|/", re.I)


def find_alternatives(text: str) -> list[list[str]]:
    """Skills joined by "or" / "/" on the same line form an alternative group."""
    groups: list[list[str]] = []
    for line in text.splitlines():
        if not _OR.search(line):
            continue
        for clause in re.split(r"[;,.](?=\s)|\band\b", line):
            if not _OR.search(clause):
                continue
            skills = extract_skills(clause)
            if len(skills) >= 2:
                groups.append(skills)
    return groups


def title_terms(title: str) -> str:
    return normalize(title)
