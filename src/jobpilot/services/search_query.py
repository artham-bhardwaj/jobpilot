"""Job search queries: what you type in the Jobs search box.

    java spring            both words, anywhere (title, company, location or description)
    "low level design"     an exact phrase
    -senior  -"on call"    must NOT contain
    title:backend          only in the title       (also company:/at:, location:/in:, skill:)
    k8s                    known skills match their synonyms too (k8s ↔ kubernetes, postgres ↔ postgresql, js ↔ …)

Words match whole words ("java" doesn't match "javascript"; "go" doesn't match "google"). Results are ranked by
where they matched: title, then skills / company, then description, then your match score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from markupsafe import Markup, escape

from jobpilot.services import skills_taxonomy
from jobpilot.services.text_utils import normalize

FIELDS = {"title": "title", "company": "company", "at": "company", "location": "location", "in": "location",
          "loc": "location", "skill": "skill", "skills": "skill"}
_TOKEN = re.compile(r'(-)?(?:(\w+):)?(?:"([^"]+)"|(\S+))')


@dataclass
class Term:
    text: str                     # as typed (normalised)
    field: str | None = None      # title | company | location | skill | None (anywhere)
    negate: bool = False
    variants: list[str] = dc_field(default_factory=list)   # the text plus its skill synonyms
    pattern: re.Pattern | None = None

    def display(self) -> str:
        t = f'"{self.text}"' if " " in self.text else self.text
        return ("-" if self.negate else "") + (f"{self.field}:" if self.field else "") + t


@dataclass
class Query:
    raw: str
    terms: list[Term]

    @property
    def positive(self) -> list[Term]:
        return [t for t in self.terms if not t.negate]

    def without(self, i: int) -> str:
        """The query string with term ``i`` removed (for the × on a query pill)."""
        return " ".join(t.display() for j, t in enumerate(self.terms) if j != i)


def _variants(text: str) -> list[str]:
    canon = skills_taxonomy.canonical(text)
    entry = skills_taxonomy.SKILLS.get(canon)
    out = [text]
    if entry:
        # Ambiguous short aliases ("go", "rest", "node", "js" …) are ordinary words too: only matched when typed.
        out += [v for v in (canon, *entry[1]) if v == text or v not in skills_taxonomy._AMBIGUOUS]
    return list(dict.fromkeys(normalize(v) for v in out if normalize(v)))


def _pattern(variants: list[str]) -> re.Pattern:
    alts = "|".join(r"\s+".join(map(re.escape, v.split())) for v in sorted(variants, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])", re.I)


def parse(raw: str | None) -> Query:
    terms = []
    for m in _TOKEN.finditer(raw or ""):
        neg, fld, phrase, word = m.groups()
        fld_key = FIELDS.get((fld or "").lower())
        text = normalize(phrase if phrase is not None else word)
        if fld and not fld_key:  # "c++:" or an unknown prefix: treat the whole thing as text
            text = normalize(f"{fld}:{phrase or word}")
        if not text or text == "-":
            continue
        variants = _variants(text)
        terms.append(Term(text=text, field=fld_key, negate=bool(neg), variants=variants, pattern=_pattern(variants)))
    return Query(raw=raw or "", terms=terms)


def _skills_of(job) -> str:
    sk = (job.match_details or {}).get("skills") or {}
    return " ".join([*(sk.get("mandatory") or []), *(sk.get("preferred") or [])])


def _fields(job) -> dict[str, str]:
    return {"title": job.title or "", "company": job.company_name or "", "location": job.location or "",
            "skill": _skills_of(job), "description": job.description_text or ""}


_WEIGHT = {"title": 8, "skill": 4, "company": 4, "location": 2, "description": 1}


def match(query: Query, job) -> tuple[bool, float, dict]:
    """(matches, rank, info). ``info``: which fields matched, and a description snippet when only it matched."""
    f = _fields(job)
    rank = 0.0
    where: set[str] = set()
    for t in query.terms:
        scope = [t.field] if t.field and t.field != "skill" else (["skill", "description", "title"] if t.field
                                                                 else ["title", "company", "location", "skill",
                                                                       "description"])
        hits = [k for k in scope if t.pattern.search(f[k])]
        if t.negate:
            if hits:
                return False, 0.0, {}
            continue
        if not hits:
            return False, 0.0, {}
        where.update(hits)
        rank += max(_WEIGHT[k] for k in hits)
    info = {"where": sorted(where)}
    if query.positive and not ({"title", "company"} & where) and "description" in where:
        info["snippet"] = snippet(f["description"], query)
    return True, rank, info


def snippet(text: str, query: Query, width: int = 90) -> Markup:
    """A short part of ``text`` around the first match, with matches highlighted."""
    first = min((m.start() for t in query.positive if (m := t.pattern.search(text))), default=None)
    if first is None:
        return Markup("")
    start = max(0, first - width // 2)
    end = min(len(text), first + width)
    # Start and end on word boundaries.
    if start:
        start = text.find(" ", start) + 1 or start
    piece = re.sub(r"\s+", " ", text[start:end]).strip()
    return Markup(("… " if start else "") + highlight(piece, query) + (" …" if end < len(text) else ""))


def highlight(text: str | None, query: Query) -> Markup:
    """``text`` HTML-escaped, with every positive term wrapped in <mark>."""
    text = text or ""
    pats = [t.pattern for t in query.positive]
    if not pats:
        return escape(text)
    spans = sorted({(m.start(), m.end()) for p in pats for m in p.finditer(text)})
    out, pos = [], 0
    for s, e in spans:
        if s < pos:
            continue
        out.append(escape(text[pos:s]))
        out.append(Markup("<mark>") + escape(text[s:e]) + Markup("</mark>"))
        pos = e
    out.append(escape(text[pos:]))
    return Markup("").join(out)


def like_prefilter(query: Query) -> list[list[str]]:
    """Lower-cased substrings at least one of which every positive term must contain (for a cheap SQL pre-filter)."""
    return [[v.lower() for v in t.variants] for t in query.positive]
