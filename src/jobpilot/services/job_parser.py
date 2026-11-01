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

_RANGE_SEP = r"(?:-|–|—|to)"
# "3-6 years of experience", "8+ years of relevant experience"
_YEARS = re.compile(
    rf"(?P<min>\d{{1,2}}(?:\.\d)?)\s*(?:\+|plus)?\s*(?:{_RANGE_SEP}\s*(?P<max>\d{{1,2}}(?:\.\d)?)\s*)?\+?\s*"
    # up to 60 characters of qualifier ("non-internship professional software development"), within one sentence
    r"(?:years?|yrs?)(?:'|’)?\s*(?:of\s+)?(?:(?:[a-z/\-+#]|\.(?!\s)|[ \t]){0,60}?)?\s*(?:experience|exp\b)",
    re.I,
)
# "0 to 12 months of experience", "6 months of experience" (converted to years)
_EXP_MONTHS = re.compile(
    rf"(?P<min>\d{{1,2}})\s*(?:\+|plus)?\s*(?:{_RANGE_SEP}\s*(?P<max>\d{{1,2}})\s*)?\+?\s*"
    r"months?(?:'|’)?\s*(?:of\s+)?(?:[a-z/\-+#. ]{0,40}?)?\s*(?:experience|exp\b)",
    re.I,
)
# Reversed order: "experience of 8–12 years", "experience: 3+ years", "experience of at least 2 years"
_YEARS_REVERSED = re.compile(
    rf"(?:experience|exp\.?)\s*(?:of|:|-|–|—)?\s*(?:at least|minimum(?: of)?|min\.?)?\s*"
    rf"(?P<min>\d{{1,2}}(?:\.\d)?)\s*(?:\+|plus)?\s*(?:{_RANGE_SEP}\s*(?P<max>\d{{1,2}}(?:\.\d)?)\s*)?\+?\s*"
    r"(?:years?|yrs?)\b",
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
# "master" alone is usually the verb ("eagerness to master a front end stack"): a degree only as Master's / Masters or
# "Master of / degree / in".
_DEGREE = re.compile(r"\b(bachelor'?s?|master(?:'s|’s|s)|master(?=\s+(?:of|degree|in)\b)|b\.\s?tech|btech|b\.e\.|b\.s\.|b\.?sc|m\.\s?tech|mtech|m\.s\.|"
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


# "… or 0 years of work experience with a PhD": a figure that applies only to holders of an advanced degree.
_ADVANCED_DEGREE_ONLY = re.compile(
    r"^[\s,]*(?:[a-z ]{0,20})?\b(?:with|holding|for candidates with|given)\s+(?:an?\s+|the\s+)?"
    r"(?:ph\.?\s?d\.?|doctorate|doctoral degree|advanced degree|graduate degree|master'?s?(?: degree)?|mba|jd|md)\b",
    re.I)


# "3+ years working with Kafka", "5+ years building APIs": a stated requirement without the word "experience". Only
# with "+"/"plus", which marks a requirement ("2 years working here" alone could be anything).
_YEARS_WORKING = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|plus)\s*(?:years?|yrs?)(?:'|’)?\s*(?:of\s+)?(?:hands[\s-]on\s+)?"
    r"(?:working|building|developing|programming|coding|designing|writing|professional|industry|hands[\s-]on)\b",
    re.I)
# A requirement bullet that starts with the figure and names "experience" later, after a long qualifier:
# "- 3+ years of computer science fundamentals (object-oriented design, …, complexity analysis) experience".
_YEARS_BULLET = re.compile(
    rf"^[\W_]{{0,4}}(?P<min>\d{{1,2}}(?:\.\d)?)\s*(?:\+|plus)?\s*(?:{_RANGE_SEP}\s*(?P<max>\d{{1,2}}(?:\.\d)?)\s*)?\+?\s*"
    r"(?:years?|yrs?)(?:'|’)?\s+of\b.{0,250}?\b(?:experience|exp)\b", re.I)
# A sentence about the employer ("With over 20 years of experience, our company …"), not about you.
_ABOUT_EMPLOYER = re.compile(r"\b(our company|the company|our firm|founded|in business|our clients|our customers|"
                             r"we have been|we've been|industry leader|our team has)\b", re.I)
_EMPLOYER_QUANTITY = re.compile(r"\b(over|more than|nearly|almost|for (?:the )?(?:past|last)?)\s*$", re.I)
_ABOUT_YOU = re.compile(r"\b(you|your|candidate|applicant|required|requirements?|must|should|need|minimum|"
                        r"at least|qualifications?|looking for|seeking|hiring|someone|engineers?|developers?)\b", re.I)
# Wording that makes a figure a wish, not a requirement (in addition to the preferred headings and markers).
_PREFERRED_FIGURE = re.compile(r"\b(valued|appreciated|an advantage|advantageous|ideally|desirable|beneficial|"
                               r"would be nice|helpful|is a bonus|preferably)\b", re.I)
_SENTENCE_END = re.compile(r"(?<![a-z]\.[a-z]\.)(?<=[.!?;])\s+|\n", re.I)  # not after "e.g." / "i.e."
# Section headings written inline ("… in office. Basic Qualifications: 7+ years … Preferred Qualifications: 5+ …").
_INLINE_HEADING = re.compile(r"(?=\b(?:basic|minimum|required|preferred|desired|additional|bonus)\s+qualifications?\s*:"
                             r"|\bnice[\s-]to[\s-]haves?\s*:|\bgood[\s-]to[\s-]haves?\s*:|\bwhat you(?:'|’)ll need\s*:)", re.I)


def _requirement_lines(text: str):
    """Each line with whether it is in a preferred / nice-to-have context (heading or inline marker), following the
    same rules as split_sections."""
    preferred_section = False
    for raw in _INLINE_HEADING.sub("\n", text).splitlines():
        line = raw.strip()
        if not line:
            continue
        head = line.split(":", 1)[0] + ":" if ":" in line[:60] else line
        if _is_heading(line) or _is_heading(head):
            h = line if _is_heading(line) else head
            if _PREFERRED_HEAD.search(h):
                preferred_section = True
            elif _OTHER_HEAD.search(h) or _REQUIRED_HEAD.search(h):
                preferred_section = False
        # Inline wishes ("… is a plus") are judged per sentence by the caller.
        yield line, preferred_section


def _figures(sentence: str) -> list[tuple[float, float | None, str, bool, int]]:
    """(min, max, snippet, for-advanced-degree-only, position) of each years/months figure in one sentence."""
    out, starts = [], set()
    pats = [(_YEARS, 1.0), (_EXP_MONTHS, 12.0), (_YEARS_REVERSED, 1.0), (_YEARS_WORKING, 1.0), (_YEARS_BULLET, 1.0)]
    for pat, per_year in pats:
        for m in pat.finditer(sentence):
            if m.start("min") in starts:  # the same figure matched by two patterns
                continue
            lo = round(float(m.group("min")) / per_year, 2)
            hi = round(float(m.group("max")) / per_year, 2) if "max" in pat.groupindex and m.group("max") else None
            if lo > 30 or (hi is not None and hi < lo):
                continue
            starts.add(m.start("min"))
            advanced_only = bool(_ADVANCED_DEGREE_ONLY.match(sentence[m.end():m.end() + 60]))
            out.append((lo, hi, m.group(0).strip(), advanced_only, m.start()))
    return sorted(out, key=lambda f: f[4])


def parse_experience(text: str) -> tuple[float | None, float | None, str | None]:
    """(min years, max years, snippet) of the stated experience requirement; (None, None, None) if none is stated.

    The requirement is the **highest** figure you must meet: "7+ years in software engineering … 2+ years with CI/CD"
    needs 7, not 2. Figures in a preferred / nice-to-have context are left out (they're used only when nothing else is
    stated), as are figures about the employer ("with over 20 years of experience, we …") and figures that apply only
    to holders of an advanced degree. Alternatives in one sentence ("3+ years with Java or 5+ years in any language")
    count as their lowest. An explicit zero ("0 to 12 months of experience") is (0.0, 1.0, …), never confused with
    "not stated". Months are converted to years (the stored unit).
    """
    required: list[tuple[float, float | None, str]] = []
    preferred: list[tuple[float, float | None, str]] = []
    for line, is_preferred in _requirement_lines(text or ""):
        for sentence in _SENTENCE_END.split(line):
            figs = _figures(sentence)
            if not figs:
                continue
            if (_ABOUT_EMPLOYER.search(sentence) and not _ABOUT_YOU.search(sentence)
                    and _EMPLOYER_QUANTITY.search(sentence[:figs[0][4]])):
                continue
            general = [f for f in figs if not f[3]] or figs
            if len(general) > 1 and re.search(r"\bor\b", sentence[general[0][4]:general[-1][4]], re.I):
                pick = min(general, key=lambda f: f[0])  # alternatives: the easiest one qualifies you
            else:
                pick = max(general, key=lambda f: f[0])
            wish = is_preferred or bool(_PREFERRED_HEAD.search(sentence) or _PREFERRED_FIGURE.search(sentence))
            (preferred if wish else required).append(pick[:3])
    if required:
        return max(required, key=lambda f: f[0])
    if preferred:  # only "preferred" figures stated: the lowest is the closest thing to a requirement
        return min(preferred, key=lambda f: f[0])
    m = _YEARS_MIN_WORDS.search(text or "")
    if m:
        return float(m.group("min")), None, m.group(0).strip()
    return None, None, None


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
# Explicit must-have wording wins over softer cues ("Familiarity with Git is required"); "not required" doesn't count.
_MUST_CUE = re.compile(r"(?<!not )\b(must|mandatory|required)\b", re.I)
# Soft spans inside a sentence: skills there are examples or interests, never requirements on their own.
_EXAMPLE_CUE = re.compile(r"(?<![a-z])(such as|e\.g\.?|eg\.|for example|for instance)(?![a-z])", re.I)
_INTEREST_CUE = re.compile(
    r"\b(interest(?:ed)?(?: (?:or|and/or|and) (?:experience|background|familiarity))? in|passion(?:ate)? (?:for|about)|"
    r"curiosity (?:about|for)|enthusiasm for|(?:eager|eagerness|willing|willingness|keen|desire) to learn|"
    # what you will learn on the job is not something you must already have ("Learn and use math, statistics…")
    r"learn and (?:use|apply)|you(?:'ll| will) learn|opportunit(?:y|ies) to learn)\b", re.I)
_SPAN_END = re.compile(r";|,\s+and\b|\.\s|$")
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
        # Don't split after abbreviations ("e.g. AWS" must stay with the sentence it qualifies).
        parts = re.split(r"(?<=[.!?;])(?<!e\.g\.)(?<!i\.e\.)(?<!etc\.)(?<!\beg\.)(?<!\bvs\.)\s+", line)
        out.extend(x.strip() for x in parts if x.strip())
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
    rank = {"required": 3, "preferred": 2, "mentioned": 1}

    def put(skill: str, kind: str, basis: str, where: str, snippet: str, qualifier: str) -> None:
        if skill not in ev or rank[kind] > rank[ev[skill]["kind"]]:
            ev[skill] = {"kind": kind, "basis": basis, "where": where, "snippet": snippet[:200], "qualifier": qualifier}

    def put_sentence(sentence: str, kind: str, basis: str, where: str) -> None:
        """Classify one sentence's skills; skills named only as examples or interests never become required."""
        head, soft = _soft_spans(sentence)
        head_skills = skills_in(head)
        qualifier = _qualifier(sentence, kind)
        for sk in head_skills:
            put(sk, kind, basis, where, sentence, qualifier)
        for span, soft_q in soft:
            for sk in skills_in(span):
                if sk not in head_skills:
                    put(sk, "preferred" if kind == "required" else kind, basis, where, sentence, soft_q)

    for sentence in _sentences(req_txt):
        soft_cue = _PREF_CUE.search(sentence) and not _MUST_CUE.search(sentence)
        put_sentence(sentence, "preferred" if soft_cue else "required", "explicit", "requirements section")
    for sentence in _sentences(pref_txt):
        put_sentence(sentence, "preferred", "explicit", "preferred / nice-to-have")
    for sentence in _sentences(other_txt):
        if not skills_in(sentence):
            continue
        if _INCIDENTAL.search(sentence):
            kind, basis, where = "mentioned", "explicit", "company/stack description (not a requirement)"
        elif _PREF_CUE.search(sentence) and not _MUST_CUE.search(sentence):
            kind, basis, where = "preferred", "explicit", "preference wording"
        elif _REQ_CUE.search(sentence) and has_req and not _MUST_CUE.search(sentence):
            # The posting states its requirements in their own section; general wording elsewhere ("we're looking
            # for engineers who bring ideas from all areas, including system design…") is not one.
            kind, basis, where = "mentioned", "explicit", "outside the requirements section (not a stated requirement)"
        elif _REQ_CUE.search(sentence):
            kind, basis, where = "required", "explicit", "requirement wording"
        elif not has_req and _RESP_CUE.search(sentence):
            kind, basis, where = "required", "inferred", "responsibilities (no requirements section)"
        elif not has_req and re.match(r"^\s*[•\-*·]", sentence):
            kind, basis, where = "required", "inferred", "listed in the posting (no requirements section)"
        else:
            kind, basis, where = "mentioned", "explicit", "mentioned in passing"
        put_sentence(sentence, kind, basis, where)
    if not has_req:
        for sk in skills_in(title):
            put(sk, "required", "inferred", "job title", title, "requirement")
    return ev, has_req


def _soft_spans(sentence: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a sentence into the part that states requirements and spans that only give examples or interests.

    "Rails, or comparable depth in another framework, such as Django or Ecto" → head keeps Rails; Django and Ecto
    are examples. "Interest or experience in AI/ML" → AI/ML is an interest. Returns (head, [(span, qualifier)]).
    """
    spans: list[tuple[int, int, str]] = []
    for rx, qualifier in ((_EXAMPLE_CUE, "example"), (_INTEREST_CUE, "interest")):
        for m in rx.finditer(sentence):
            if sentence[:m.start()].count("(") > sentence[:m.start()].count(")"):
                close = sentence.find(")", m.end())
                end = close if close != -1 else len(sentence)
            else:
                end = m.end() + _SPAN_END.search(sentence, m.end()).start() - m.end()
            spans.append((m.start(), end, qualifier))
    if not spans:
        return sentence, []
    head = list(sentence)
    for start, end, _ in spans:
        head[start:end] = " " * (end - start)
    return "".join(head), [(sentence[start:end], q) for start, end, q in spans]


def _qualifier(sentence: str, kind: str) -> str:
    """How strongly the sentence asks for its (non-example) skills."""
    if kind == "mentioned":
        return "mention"
    if kind == "required":
        return "requirement"
    low = sentence.lower()
    if "familiar" in low:
        return "familiarity"
    if "exposure" in low:
        return "exposure"
    if _INTEREST_CUE.search(sentence):
        return "interest"
    return "preference"


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
        p.evidence["deadline"] = _ev(p.application_deadline, "structured",
                                     f"source data ({structured.get('deadline_source') or 'validThrough'})")
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
