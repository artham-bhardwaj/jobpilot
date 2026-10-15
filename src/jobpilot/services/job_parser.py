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
    r"(requirements?|required|qualifications|must[\s-]have|what you.ll need|what we.re looking for|"
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
    all_skills: list[str] = field(default_factory=list)
    experience_min_years: float | None = None
    experience_max_years: float | None = None
    experience_evidence: str | None = None
    experience_level: str | None = None
    remote_type: str = "unknown"
    sponsorship: str = "not_stated"  # not_available | available | not_stated
    work_authorization_required: bool = False
    location_constraints: list[str] = field(default_factory=list)
    relocation_offered: bool = False
    degree_required: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    emails: list[str] = field(default_factory=list)
    # Groups of interchangeable skills stated on one line ("Django or FastAPI").
    alternatives: list[list[str]] = field(default_factory=list)

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


def parse(title: str, description: str | None, location: str | None = None) -> ParsedRequirements:
    text = description or ""
    p = ParsedRequirements()
    req_txt, pref_txt, other_txt = split_sections(text)
    preferred = extract_skills(pref_txt)
    required = extract_skills(req_txt)
    if not req_txt.strip():
        # No requirements section: skills outside "preferred" text are treated as required.
        required = [s for s in extract_skills(other_txt + "\n" + title) if s not in preferred]
    p.mandatory_skills = required
    p.preferred_skills = [s for s in preferred if s not in required]
    p.all_skills = list(dict.fromkeys(required + p.preferred_skills + extract_skills(other_txt) + extract_skills(title)))

    p.experience_min_years, p.experience_max_years, p.experience_evidence = parse_experience(text)
    p.experience_level = level_from(title, p.experience_min_years)
    p.remote_type = remote_from(title, location, text[:3000])

    if _SPONSOR_NO.search(text):
        p.sponsorship = "not_available"
    elif _SPONSOR_YES.search(text):
        p.sponsorship = "available"
    p.work_authorization_required = bool(_AUTH_REQ.search(text))
    p.location_constraints = list(dict.fromkeys(m.group(1).strip() for m in _LOCATED.finditer(text)))[:5]
    p.relocation_offered = bool(_RELOCATE.search(text))
    d = _DEGREE.search(req_txt or text)
    if d:
        p.degree_required = d.group(0).strip()[:120]
    p.salary_min, p.salary_max, p.salary_currency = parse_salary(text)
    p.emails = sorted(set(_EMAIL.findall(text)))
    p.alternatives = find_alternatives(text)
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
