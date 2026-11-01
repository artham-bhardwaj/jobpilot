"""Job-specific resumes, generated locally from the approved Master resume's own LaTeX source.

How a tailored resume is made (nothing is written by a model, nothing is invented):

1. The **template** is the LaTeX source of your approved Master resume, registered once (``jobpilot resume template
   --file …``). Registration checks that its text is the approved version's text, word for word; the copy is kept
   read-only with its checksum, and the Master resume's record and file are never touched.
2. Every bullet, project and skill in the template is **mapped to a confirmed profile fact** (employment and
   project description lines, skills, certifications, education). Employer, title and dates must equal your
   confirmed employment record. Anything that maps only to an unconfirmed fact is left out; nothing is added.
3. **Tailoring = re-ordering**: experience bullets, projects (and their bullets) and the skills inside each skills
   line are ordered by how many of the job's required / preferred skills they actually mention. Wording, metrics,
   dates, employers and titles are copied verbatim. Job requirements you don't meet are reported, never added.
   Education years are the one exception: they are taken from your confirmed education records (recorded as
   corrections; e.g. a template showing 2021 for an exam your record says you passed in 2020).
4. The document is compiled **locally** (latexmk or pdflatex; no network, no shell escape, a timeout, a temporary
   build directory) and **validated**: text extractable, every expected line present (nothing clipped or lost),
   the same words as the approved Master resume (minus anything left out), no placeholders or LaTeX errors, no blank
   pages, the page target met, every word inside the page.
5. A valid PDF is stored as ``tailored_resumes/<company>_<job id>_<hash>.pdf`` and is attached to that job's
   application instead of the Master resume (``attachment_for``), after the same checks again. Unchanged inputs reuse
   the existing PDF.

Resumes are generated only when you ask for one (the job's page, or ``jobpilot resume generate``), for any job.
Nothing generates them in the background.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import (Achievement, Application, Certification, Education, Employment, Job, Project, Resume,
                             ResumeVersion, Skill, utcnow)
from jobpilot.models.tailored import TailoredResume
from jobpilot.services import audit, settings_store
from jobpilot.services.skills_taxonomy import extract_skills
from jobpilot.services.text_utils import normalize, normalize_company

log = logging.getLogger(__name__)

SETTINGS_KEY = "tailored_resumes"
TEMPLATE_DIR = "templates"
OUTPUT_DIR = "tailored_resumes"
SOURCE_DATE_EPOCH = "1767225600"  # fixed PDF dates (2026-01-01): the same source gives the same PDF bytes


class TailorError(ValueError):
    """Generation can't proceed (missing template, unconfirmed facts, compiler unavailable …)."""


class CompilerUnavailable(TailorError):
    pass


def cfg(session: Session) -> dict:
    return settings_store.get(session, SETTINGS_KEY)


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


# ---- LaTeX structure: itemize trees that round-trip byte for byte -----------------------------------------------

_TOKEN = re.compile(r"\\begin\{itemize\}|\\end\{itemize\}|\\item(?![A-Za-z@])")


def _in_comment(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return bool(re.search(r"(?<!\\)%", text[line_start:pos]))


@dataclass
class Env:
    prefix: str  # "\begin{itemize}…" up to the first \item
    items: list[Item] = field(default_factory=list)
    suffix: str = "\\end{itemize}"

    def render(self) -> str:
        return self.prefix + "".join(i.render() for i in self.items) + self.suffix


@dataclass
class Item:
    parts: list[str | Env] = field(default_factory=list)

    def render(self) -> str:
        return "".join(p if isinstance(p, str) else p.render() for p in self.parts)

    @property
    def text(self) -> str:
        """This item's own LaTeX (nested lists excluded)."""
        return "".join(p for p in self.parts if isinstance(p, str))

    @property
    def child(self) -> Env | None:
        return next((p for p in self.parts if isinstance(p, Env)), None)


def _tokens(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0)) for m in _TOKEN.finditer(text) if not _in_comment(text, m.start())]


def _parse_env(text: str, toks: list, i: int) -> tuple[Env, int, int]:
    """Parse from token i (a \\begin{itemize}); returns (env, next token index, end offset)."""
    start = toks[i][0]
    i += 1
    first = toks[i][0] if i < len(toks) else len(text)
    env = Env(prefix=text[start:first])
    cur: Item | None = None
    pos = first
    while i < len(toks):
        off, tok = toks[i]
        if tok == "\\begin{itemize}":
            if cur is None:
                raise TailorError("unexpected nested list before the first \\item")
            cur.parts.append(text[pos:off])
            child, i, pos = _parse_env(text, toks, i)
            cur.parts.append(child)
            continue
        if cur is not None:
            cur.parts.append(text[pos:off])
        if tok == "\\item":
            cur = Item()
            env.items.append(cur)
            pos = off
            i += 1
            continue
        end = off + len(tok)  # \end{itemize}
        env.suffix = tok
        if cur is None:
            env.prefix = text[start:off]
        return env, i + 1, end
    raise TailorError("unbalanced itemize environment in the template")


@dataclass
class Section:
    heading: str  # "EXPERIENCE"
    head: str  # text before the list
    env: Env | None
    tail: str

    def render(self) -> str:
        return self.head + (self.env.render() if self.env else "") + self.tail


@dataclass
class Document:
    preamble: str  # up to and including the header, before the first section
    sections: list[Section]

    def render(self) -> str:
        return self.preamble + "".join(s.render() for s in self.sections)

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.heading == name), None)


_HEADING = re.compile(r"\\resheading\{(?:\\textbf\{)?([^{}]+)\}?\}")


def parse(tex: str) -> Document:
    starts = [m for m in _HEADING.finditer(tex) if not _in_comment(tex, m.start())]
    if not starts:
        raise TailorError("the template has no \\resheading{…} sections")
    # Each section begins at the separator comment line before its heading, if any.
    cuts = []
    for m in starts:
        line = tex.rfind("\n", 0, m.start()) + 1
        prev = tex.rfind("\n", 0, max(line - 1, 0)) + 1
        cuts.append(prev if tex[prev:line].lstrip().startswith("%---") else line)
    sections = []
    for n, (m, cut) in enumerate(zip(starts, cuts)):
        end = cuts[n + 1] if n + 1 < len(cuts) else tex.find("\\end{document}", cut)
        if end < 0:
            raise TailorError("the template has no \\end{document}")
        chunk = tex[cut:end]
        toks = _tokens(chunk)
        if toks and toks[0][1] == "\\begin{itemize}":
            env, _, stop = _parse_env(chunk, toks, 0)
            sections.append(Section(m.group(1).strip().upper(), chunk[:toks[0][0]], env, chunk[stop:]))
        else:
            sections.append(Section(m.group(1).strip().upper(), chunk, None, ""))
    doc = Document(tex[:cuts[0]], sections)
    last_end = tex.find("\\end{document}", cuts[-1])
    sections[-1].tail += tex[last_end:]
    if doc.render() != tex:
        raise TailorError("the template could not be read losslessly; nothing generated")
    return doc


# ---- plain text of LaTeX snippets (for matching and validation) --------------------------------------------------

_CMD_ARG = re.compile(r"\\(?:textbf|textit|emph|underline|small|large|Large|noindent)\b\s*")


def plain(latex: str) -> str:
    s = re.sub(r"(?<!\\)%.*", "", latex)
    s = re.sub(r"\\(?:begin|end)\{[^{}]*\}(\[[^\]]*\])?", " ", s)
    s = re.sub(r"\\(?:vspace|hspace)\*?\{[^{}]*\}", " ", s)
    s = re.sub(r"\\href\{[^{}]*\}\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\\\(\[[^\]]*\])?", " ", s)
    s = re.sub(r"\$\\(?:cdot|circ)\$", " ", s)
    s = s.replace("---", "—").replace("--", "–").replace("\\&", "&").replace("\\%", "%").replace("\\$", "$")
    s = re.sub(r"\\(?:item|hfill|enspace|itemsep|isep|lsep)\b", " ", s)
    s = _CMD_ARG.sub("", s)
    s = re.sub(r"\\[A-Za-z]+\*?", " ", s)
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


def alnum(text: str) -> str:
    """Letters and digits only, lower-case: robust to line breaks, hyphenation and bullet glyphs in extracted text."""
    t = (text or "").replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi").replace("ﬄ", "ffl")
    return re.sub(r"[^0-9a-z]+", "", normalize(t))


# ---- the registered template ----------------------------------------------------------------------------------------

def master_version(session: Session) -> ResumeVersion | None:
    session.flush()  # a version approved in this session counts
    return session.scalar(select(ResumeVersion).join(Resume).where(
        Resume.is_master.is_(True), Resume.active.is_(True), ResumeVersion.status == "approved")
        .order_by(ResumeVersion.version_no.desc(), ResumeVersion.id.desc()).limit(1))


def template_info(session: Session) -> dict | None:
    return cfg(session).get("template")


def register_template(session: Session, settings: Settings, source: Path, version: ResumeVersion | None = None) -> dict:
    """Copy the LaTeX source of the approved Master resume into JobPilot (read-only), after checking that its text
    is the approved version's text. The source file and the Master resume are not changed."""
    version = version or master_version(session)
    if version is None or version.status != "approved":
        raise TailorError("No approved Master resume version to register a template for")
    data = Path(source).read_bytes()
    tex = data.decode("utf-8")
    doc = parse(tex)
    words = alnum(plain(tex[tex.find("\\begin{document}"):]))
    master_words = alnum(version.extracted_text or "")
    if not master_words or sorted(words) != sorted(master_words):
        raise TailorError("The template's text differs from the approved Master resume's text; not registered "
                          "(use the exact LaTeX source of the approved version)")
    target_dir = settings.files_dir / TEMPLATE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = _sha(data)
    target = target_dir / f"master_v{version.version_no}_{digest[:12]}.tex"
    if not target.exists():
        target.write_bytes(data)
        target.chmod(0o400)
    info = {"path": f"{TEMPLATE_DIR}/{target.name}", "sha256": digest, "source_version_id": version.id,
            "original_name": Path(source).name, "registered_at": utcnow().isoformat(timespec="seconds"),
            "sections": [s.heading for s in doc.sections]}
    settings_store.put(session, SETTINGS_KEY, {**cfg(session), "template": info})
    # Tailored resumes made from another template or Master version are outdated: never attached, and the
    # scheduler regenerates them in its usual batches. Their files and records are kept.
    outdated = 0
    for tr in session.scalars(select(TailoredResume).where(TailoredResume.status == "valid")):
        if tr.template_sha256 != digest or tr.source_version_id != version.id:
            tr.status = "outdated"
            tr.error = (f"Outdated: made from Master resume version #{tr.source_version_id}; the template is now "
                        f"version {version.version_no}'s source")
            outdated += 1
    audit.record(session, "tailored.template_registered", "resume_version", version.id, sha256=digest[:16],
                 outdated=outdated)
    return {**info, "outdated_tailored_resumes": outdated}


def load_template(session: Session, settings: Settings) -> tuple[str, dict, ResumeVersion]:
    info = template_info(session)
    if not info:
        raise TailorError("No resume template registered. Register the LaTeX source of your approved Master resume: "
                          "jobpilot resume template --file <path/to/resume.tex>")
    path = settings.files_dir / info["path"]
    if not path.is_file():
        raise TailorError(f"The registered template is missing ({info['path']})")
    data = path.read_bytes()
    if _sha(data) != info["sha256"]:
        raise TailorError("The registered template's checksum changed; register it again")
    version = master_version(session)
    if version is None or version.id != info["source_version_id"]:
        raise TailorError("The template belongs to a Master resume version that is no longer the approved one; "
                          "register the new version's LaTeX source")
    return data.decode("utf-8"), info, version


# ---- confirmed facts --------------------------------------------------------------------------------------------

def _lines(text: str | None) -> list[str]:
    return [ln.strip(" •-*\t\r") for ln in (text or "").splitlines() if ln.strip(" •-*\t\r")]


def _month_range(start, end) -> str:
    s = start.strftime("%b %Y") if start else ""
    return f"{s} – {end.strftime('%b %Y') if end else 'Present'}" if s else ""


def facts(session: Session) -> dict[str, Any]:
    conf = lambda model: list(session.scalars(select(model).where(model.status == "confirmed").order_by(model.id)))  # noqa: E731
    unconf = lambda model: list(session.scalars(select(model).where(model.status != "confirmed").order_by(model.id)))  # noqa: E731
    out = {"employment": conf(Employment), "projects": conf(Project), "skills": conf(Skill),
           "certifications": conf(Certification), "education": conf(Education), "achievements": conf(Achievement),
           "unconfirmed": {"employment": unconf(Employment), "projects": unconf(Project), "skills": unconf(Skill)}}
    snapshot = {k: [[r.id, getattr(r, "updated_at", None) and r.updated_at.isoformat(), r.status]
                    for r in v] for k, v in out.items() if k != "unconfirmed"}
    out["sha256"] = _sha(json.dumps(snapshot, sort_keys=True, default=str))
    return out


# ---- tailoring ------------------------------------------------------------------------------------------------------

def job_skills(job: Job) -> dict[str, list[str]]:
    sk = (job.match_details or {}).get("skills") or {}
    mand, pref = list(sk.get("mandatory") or []), list(sk.get("preferred") or [])
    other = [s for s in (sk.get("mentioned") or []) if s not in mand and s not in pref]
    if not (mand or pref or other):  # not matched yet: what the description mentions
        other = extract_skills(job.description_text or "", ignore=[job.company_name] if job.company_name else None)
    return {"required": mand, "preferred": pref, "mentioned": other}


def _score(text: str, js: dict[str, list[str]]) -> tuple[float, list[str]]:
    found = set(extract_skills(text))
    hits = [s for s in js["required"] + js["preferred"] + js["mentioned"] if s in found]
    w = sum(3 if s in js["required"] else 1.5 if s in js["preferred"] else 1 for s in set(hits))
    return w, sorted(set(hits))


def _reorder(items: list, key) -> tuple[list, bool]:
    scored = [(i, key(it)) for i, it in enumerate(items)]
    new = [items[i] for i, _ in sorted(scored, key=lambda x: (-x[1], x[0]))]
    return new, [id(x) for x in new] != [id(x) for x in items]


def _split_values(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "(" or ch == "{"
        depth -= ch == ")" or ch == "}"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


_SKILL_LINE = re.compile(r"^(\\item\s*(?:\\noindent\s*)?\\textbf\{[^{}]*:\}\s*)(.*?)(\s*)$", re.S)


def _skill_match(token: str, confirmed: dict[str, Skill]) -> Skill | None:
    plain_t = plain(token)
    n = normalize(re.sub(r"\(.*?\)", "", plain_t)).strip()
    if n in confirmed:
        return confirmed[n]
    for s in extract_skills(plain_t):
        if s in confirmed:
            return confirmed[s]
    for name, sk in confirmed.items():
        if len(name) >= 3 and (name in n or n in name):
            return sk
    return None


_DATE_SPAN = re.compile(r"\\textit\{([^{}]*)\}")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _confirmed_years(item: Item, record: Education) -> tuple[str, str] | tuple[()] | None:
    """Make the years in an education entry's date (``\\textit{2021}`` or ``\\textit{2021 -- 2025}``) those of the
    confirmed record. Returns (old, new) when changed, () when already right, None when it can't be checked."""
    for n, part in enumerate(item.parts):
        if not isinstance(part, str):
            continue
        spans = [m for m in _DATE_SPAN.finditer(part) if _YEAR.search(m.group(1))]
        if not spans:
            continue
        m = spans[-1]
        years = _YEAR.findall(m.group(1))
        if len(years) == 1 and record.end_date:
            want = [str(record.end_date.year)]  # a single year is the year it was completed
        elif len(years) == 2 and record.start_date and record.end_date:
            want = [str(record.start_date.year), str(record.end_date.year)]
        else:
            return None
        if years == want:
            return ()
        it = iter(want)
        new_span = _YEAR.sub(lambda _: next(it), m.group(1))
        item.parts[n] = part[:m.start(1)] + new_span + part[m.end(1):]
        return plain(m.group(1)), plain(new_span)
    return None


def tailor(session: Session, settings: Settings, job: Job, tex: str | None = None) -> dict[str, Any]:
    """Re-order the template's content for ``job``. Returns {tex, decisions, expected, excluded}. Raises TailorError."""
    if tex is None:
        tex, _, _ = load_template(session, settings)
    doc = parse(tex)
    f = facts(session)
    js = job_skills(job)
    confirmed = {s.normalized_name: s for s in f["skills"]}
    decisions: dict[str, Any] = {"job": {"id": job.id, "title": job.title, "company": job.company_name},
                                 "job_skills": js, "sections": {}, "mapping": [], "excluded": [], "warnings": [],
                                 "not_claimed": []}
    expected: list[str] = []  # plain text of every content line that must appear in the PDF
    excluded_full: list[str] = []  # the complete text of everything left out (for the content comparison)

    emp_lines = {alnum(ln): (e, i) for e in f["employment"] for i, ln in enumerate(_lines(e.description))}
    unconf_emp = {alnum(ln) for e in f["unconfirmed"]["employment"] for ln in _lines(e.description)}
    proj_lines = {alnum(ln): (p, i) for p in f["projects"] for i, ln in enumerate(_lines(p.description))}

    def bullets(env: Env, lookup: dict, what: str, owner: str) -> None:
        kept = []
        for it in env.items:
            t = plain(it.text)
            hit = lookup.get(alnum(t))
            if hit is None:
                reason = ("matches an unconfirmed fact" if alnum(t) in unconf_emp else
                          "not found in any confirmed profile fact")
                decisions["excluded"].append({"section": what, "text": t[:160], "reason": reason})
                excluded_full.append(t)
                continue
            kept.append(it)
            score, hits = _score(t, js)
            decisions["mapping"].append({"section": what, "owner": owner, "text": t[:160],
                                         "fact": f"{type(hit[0]).__name__.lower()}#{hit[0].id} line {hit[1] + 1}",
                                         "score": score, "job_skills": hits})
            expected.append(t)
        env.items, changed = _reorder(kept, lambda it: _score(plain(it.text), js)[0])
        decisions["sections"].setdefault(what, []).append(
            {"owner": owner, "reordered": changed, "order": [plain(i.text)[:60] for i in env.items]})

    # EXPERIENCE: each job must be a confirmed employment record with the same employer, title and dates.
    exp = doc.section("EXPERIENCE")
    if exp and exp.env:
        for it in exp.env.items:
            head = plain(it.text)
            emp = next((e for e in f["employment"] if normalize_company(e.company) in normalize_company(head)
                        and normalize(e.title) in normalize(head)), None)
            if emp is None:
                raise TailorError(f"Experience entry “{head[:80]}” is not a confirmed employment record; not generated")
            if alnum(_month_range(emp.start_date, emp.end_date)) not in alnum(head):
                raise TailorError(f"The dates of “{head[:80]}” differ from your confirmed employment record "
                                  f"({_month_range(emp.start_date, emp.end_date)}); not generated")
            expected.append(head)
            decisions["mapping"].append({"section": "EXPERIENCE", "owner": emp.company, "text": head[:160],
                                         "fact": f"employment#{emp.id} (company, title, dates)"})
            if it.child:
                bullets(it.child, emp_lines, "EXPERIENCE", emp.company)

    # PROJECTS: confirmed projects only; projects and their bullets ordered by relevance.
    prj = doc.section("PROJECTS")
    if prj and prj.env:
        kept = []
        for it in prj.env.items:
            head = plain(it.text)
            p = next((p for p in f["projects"] if normalize(p.name) in normalize(head)), None)
            if p is None:
                decisions["excluded"].append({"section": "PROJECTS", "text": head[:160],
                                              "reason": "not a confirmed project"})
                excluded_full.append(plain(it.render()))
                continue
            kept.append((it, p))
            expected.append(head)
            decisions["mapping"].append({"section": "PROJECTS", "owner": p.name, "text": head[:160],
                                         "fact": f"project#{p.id} (name, dates, technologies)"})
            if it.child:
                bullets(it.child, {k: v for k, v in proj_lines.items() if v[0].id == p.id}, "PROJECTS", p.name)
        ordered, changed = _reorder(kept, lambda x: _score(plain(x[0].render()), js)[0])
        prj.env.items = [it for it, _ in ordered]
        decisions["sections"]["PROJECTS order"] = {"reordered": changed, "order": [p.name for _, p in ordered]}

    # TECHNICAL SKILLS: the same skills, the job's first within each line. Never a skill that isn't there.
    sk = doc.section("TECHNICAL SKILLS")
    if sk and sk.env:
        for it in sk.env.items:
            m = _SKILL_LINE.match(it.text) if len(it.parts) == 1 else None
            if not m:
                expected.append(plain(it.text))
                continue
            values = _split_values(m.group(2))
            lead = [v for v in values if v.strip()]
            mapped = [(v, _skill_match(v, confirmed)) for v in lead]
            for v, s in mapped:
                decisions["mapping"].append({"section": "TECHNICAL SKILLS", "text": plain(v),
                                             "fact": f"skill#{s.id} {s.name}" if s else
                                             "approved Master resume wording (no separate profile skill record)"})
            rel = {id(v): _score(plain(v), js)[0] for v in lead}
            ordered, changed = _reorder(lead, lambda v: rel[id(v)])
            if changed:
                it.parts = [m.group(1) + ", ".join(v.strip() for v in ordered) + m.group(3)]
            expected.append(plain(it.text))
            decisions["sections"].setdefault("TECHNICAL SKILLS", []).append(
                {"line": plain(m.group(1)), "reordered": changed, "order": [plain(v) for v in ordered]})

    # Other sections (achievements, certifications, education) are copied as they are, after checking them.
    for name, rows, label in (("CERTIFICATIONS", f["certifications"], "name"),
                              ("ACHIEVEMENTS", f["achievements"], "title")):
        s = doc.section(name)
        if s and s.env:
            for it in s.env.items:
                t = plain(it.text)
                r = next((r for r in rows if alnum(getattr(r, label))[:40] in alnum(t)), None)
                if r is None:
                    decisions["warnings"].append(f"{name}: “{t[:80]}” has no confirmed profile record")
                decisions["mapping"].append({"section": name, "text": t[:160],
                                             "fact": f"{name.lower()[:-1]}#{r.id}" if r else "approved Master resume"})
                expected.append(t)
    # EDUCATION: copied as it is, except that the years shown are those of your confirmed education record.
    corrections: list[tuple[str, str]] = []  # (old text, new text) — the content comparison allows exactly these
    decisions["corrections"] = []
    edu = doc.section("EDUCATION")
    if edu and edu.env:
        for it in edu.env.items:
            t = plain(it.text)
            r = next((r for r in f["education"] if alnum(r.institution) in alnum(t)), None)
            if r is None:
                decisions["warnings"].append(f"EDUCATION: “{t[:80]}” has no confirmed education record")
            else:
                fixed = _confirmed_years(it, r)
                if fixed is None:
                    if r.end_date is None or str(r.end_date.year) not in t:
                        decisions["warnings"].append(f"EDUCATION: the years of “{t[:60]}” can't be checked against "
                                                     "your confirmed record; copied unchanged")
                elif fixed:
                    old_years, new_years = fixed
                    corrections.append((old_years, new_years))
                    decisions["corrections"].append({"section": "EDUCATION", "entry": t[:80], "from": old_years,
                                                     "to": new_years, "fact": f"education#{r.id}"})
                    t = plain(it.text)
            decisions["mapping"].append({"section": "EDUCATION", "text": t[:160],
                                         "fact": f"education#{r.id}" if r else "approved Master resume"})
            expected.append(t)
    if "SUMMARY" not in [s.heading for s in doc.sections]:
        decisions["warnings"].append("The approved template has no summary section, so none is added (the approved "
                                     "structure is kept)")
    have = set(confirmed)
    decisions["not_claimed"] = sorted(set(js["required"] + js["preferred"]) - have)
    out = doc.render()
    # Rewording: the posting's spelling of skills you already list (pure synonyms only; see services/wording.py).
    decisions["rewording"] = []
    if cfg(session).get("reword_skills", True) and job.description_text:
        from jobpilot.services import wording

        cut = out.find("\\begin{document}")
        head, body = (out[:cut], out[cut:]) if cut >= 0 else ("", out)
        swaps = wording.plan(plain(body), f"{job.title or ''}\n{job.description_text}")
        if swaps:
            body, done = wording.apply(body, swaps)
            out = head + body
            corrections.extend(done)
            expected = [wording.apply(e, swaps)[0] for e in expected]
            counts: dict[tuple[str, str], int] = {}
            for pair in done:
                counts[pair] = counts.get(pair, 0) + 1
            decisions["rewording"] = [{"from": a, "to": b, "count": n} for (a, b), n in counts.items()]
    return {"tex": out, "decisions": decisions, "expected": expected, "excluded": decisions["excluded"],
            "excluded_full": excluded_full, "corrections": corrections,
            "header": plain(doc.preamble[doc.preamble.find("\\begin{document}"):])}


# ---- compiling --------------------------------------------------------------------------------------------------

def compiler(settings: Settings | None = None) -> list[str] | None:
    """The local LaTeX command, or None. latexmk preferred; pdflatex otherwise."""
    if shutil.which("latexmk") and shutil.which("pdflatex"):
        return ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "-quiet"]
    if shutil.which("pdflatex"):
        return ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape"]
    return None


INSTALL_HINT = ("Install a local LaTeX compiler: sudo apt install latexmk texlive-latex-recommended "
                "texlive-fonts-recommended")


def compile_pdf(tex: str, timeout: int = 90, runner=subprocess.run) -> bytes:
    """Compile ``tex`` in a temporary directory; returns the PDF bytes. Raises CompilerUnavailable / TailorError."""
    cmd = compiler()
    if cmd is None:
        raise CompilerUnavailable(f"No LaTeX compiler found. {INSTALL_HINT}")
    with tempfile.TemporaryDirectory(prefix="jobpilot-tex-") as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(tex, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
               "FORCE_SOURCE_DATE": "1", "openout_any": "p", "openin_any": "p", "TEXMFOUTPUT": tmp, "LANG": "C.UTF-8"}
        try:
            proc = runner(cmd + ["resume.tex"], cwd=tmp, env=env, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise TailorError(f"LaTeX compilation timed out after {timeout} s") from exc
        pdf = Path(tmp) / "resume.pdf"
        if proc.returncode != 0 or not pdf.is_file():
            logf = Path(tmp) / "resume.log"
            lines = logf.read_text(errors="replace").splitlines() if logf.is_file() else \
                (proc.stdout or b"").decode(errors="replace").splitlines()
            errs = [ln for ln in lines if ln.startswith("!") or "Error" in ln][:8]
            raise TailorError("LaTeX compilation failed: " + ("; ".join(errs)[:600] or f"exit code {proc.returncode}"))
        return pdf.read_bytes()


# ---- validating ---------------------------------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{\{|\}\}|<<|>>|\bTODO\b|\bTBD\b|\bPLACEHOLDER\b|\bXXX\b|\?\?|\\[A-Za-z]{2,}")
LATEX_ERRORS = re.compile(r"LaTeX Error|Undefined control sequence|Missing \$ inserted|Overfull \\hbox", re.I)


def _pages_text(path: Path, pages: int) -> list[str]:
    out = []
    for n in range(1, pages + 1):
        r = subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), "-layout", str(path), "-"], capture_output=True,
                           timeout=30)
        out.append(r.stdout.decode(errors="replace") if r.returncode == 0 else "")
    return out


def _out_of_page(path: Path) -> int:
    """Words whose box leaves the page (clipped text), from pdftotext -bbox."""
    r = subprocess.run(["pdftotext", "-bbox", str(path), "-"], capture_output=True, timeout=30)
    if r.returncode != 0:
        return -1
    html = r.stdout.decode(errors="replace")
    bad = 0
    for page in re.finditer(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', html, re.S):
        w, h = float(page.group(1)), float(page.group(2))
        for m in re.finditer(r'xMin="([\d.-]+)" yMin="([\d.-]+)" xMax="([\d.-]+)" yMax="([\d.-]+)"', page.group(3)):
            x0, y0, x1, y1 = map(float, m.groups())
            bad += x0 < 0 or y0 < 0 or x1 > w or y1 > h
    return bad


def validate(session: Session, settings: Settings, pdf: Path, *, expected: list[str], header: str, job: Job,
             master: ResumeVersion, excluded_text: list[str], page_target: int | None,
             corrections: list | tuple = ()) -> dict[str, Any]:
    from jobpilot.services import resumes as resume_svc

    checks: dict[str, Any] = {}
    errors: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), **({"detail": detail} if detail else {})}
        if not ok:
            errors.append(f"{name}: {detail}" if detail else name)

    data = pdf.read_bytes() if pdf.is_file() else b""
    check("file_exists", bool(data), "the PDF is missing or empty")
    if not data:
        return {"ok": False, "checks": checks, "errors": errors}
    ex = resume_svc.extract_text("resume.pdf", data)
    text = ex.get("text") or ""
    check("parsable", ex.get("ok", False) and ex.get("page_count"), "; ".join(ex.get("errors") or []) or "unreadable")
    check("text_extractable", len(text.strip()) > 200, "no extractable text")
    pages = ex.get("page_count") or 0
    target = page_target or master.page_count
    check("page_count", bool(pages) and (target is None or pages <= target),
          f"{pages} page(s); target {target}")
    blank = [i + 1 for i, t in enumerate(_pages_text(pdf, pages)) if len(t.strip()) < 40] if pages else []
    check("no_blank_pages", not blank, f"blank page(s): {blank}")
    body = alnum(text)
    missing = [e for e in expected if alnum(e) and alnum(e) not in body]
    check("all_content_present", not missing, f"{len(missing)} line(s) missing or clipped, e.g. “{missing[0][:80]}”"
          if missing else "")
    p = master_profile_strings(session)
    lost = [s for s in p if alnum(s) not in body]
    check("candidate_details", not lost, f"missing: {', '.join(lost)}")
    sections = [s for s in ("EXPERIENCE", "TECHNICAL SKILLS", "PROJECTS", "EDUCATION") if alnum(s) in alnum(master.extracted_text or "")]
    check("sections_present", all(alnum(s) in body for s in sections), "a section heading is missing")
    leftover = PLACEHOLDER.findall(text)
    check("no_placeholders", not leftover, f"found {sorted(set(leftover))[:5]}")
    check("no_latex_errors", not LATEX_ERRORS.search(text), "LaTeX error text in the document")
    # Same words as the approved Master resume, minus whatever was left out: nothing added, nothing lost.
    master_chars = sorted(alnum(master.extracted_text or ""))
    want = list(master_chars)
    for t in excluded_text:
        for ch in alnum(t):
            if ch in want:
                want.remove(ch)
    for old, new in corrections:  # years replaced by your confirmed education record, and nothing else
        for ch in alnum(old):
            if ch in want:
                want.remove(ch)
        want += list(alnum(new))
    check("same_content_as_master", sorted(body) == sorted(want),
          "the text differs from the approved Master resume's (beyond re-ordering and confirmed-fact corrections)")
    clipped = _out_of_page(pdf)
    check("inside_page", clipped == 0, f"{clipped} word(s) outside the page" if clipped > 0 else "bbox check failed")
    try:
        resume_svc.verified_file(settings, master)
        check("master_unchanged", True)
    except resume_svc.ResumeFileError as exc:
        check("master_unchanged", False, str(exc))
    return {"ok": not errors, "checks": checks, "errors": errors, "page_count": pages}


def master_profile_strings(session: Session) -> list[str]:
    from jobpilot.services import profile as profile_svc

    p = profile_svc.get_profile(session)
    return [x for x in (p.full_name, p.email, p.phone) if x]


# ---- the pipeline -------------------------------------------------------------------------------------------------

def eligible(session: Session, job: Job) -> tuple[bool, str]:
    """Resumes are made only when you ask, so any job qualifies. The reason is a note for you (shown next to the
    button), e.g. that a short description gives the re-ordering little to go on."""
    from jobpilot.services import applications as apps

    notes = []
    if job.status != "active":
        notes.append(f"the listing is {job.status}")
    if job.eligibility == "ineligible" or job.hard_conflict:
        notes.append("the job looks ineligible for you")
    if len((job.description_text or "").strip()) < int(cfg(session)["min_description_chars"]):
        notes.append("its description is short, so the order follows the job title only")
    app = apps.for_job(session, job.id)
    if app is not None and app.applied_at is not None:
        notes.append(f"you already applied (application #{app.id})")
    return True, "; ".join(notes) or "ready"


def latest(session: Session, job_id: int, status: str | None = None) -> TailoredResume | None:
    stmt = select(TailoredResume).where(TailoredResume.job_id == job_id)
    if status:
        stmt = stmt.where(TailoredResume.status == status)
    return session.scalar(stmt.order_by(TailoredResume.id.desc()))


def _inputs(info: dict, f: dict, job: Job, page_target) -> tuple[str, str]:
    job_hash = _sha(json.dumps({"text": job.description_text or "", "skills": job_skills(job),
                                "title": job.title, "company": job.company_name}, sort_keys=True))
    return job_hash, _sha(json.dumps([info["sha256"], info["source_version_id"], f["sha256"], job_hash, page_target,
                                      PIPELINE_VERSION]))


PIPELINE_VERSION = "2"  # 2: education years follow your confirmed records


def _slug(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(s or "company")).strip("-")[:40] or "company"


def file_ok(settings: Settings, tr: TailoredResume) -> bool:
    if not tr.pdf_path or not tr.pdf_sha256:
        return False
    path = settings.files_dir / tr.pdf_path
    return path.is_file() and _sha(path.read_bytes()) == tr.pdf_sha256


def preview(session: Session, settings: Settings, job: Job) -> dict[str, Any]:
    ok, why = eligible(session, job)
    res = tailor(session, settings, job)
    return {"eligible": ok, "reason": why, "decisions": res["decisions"], "compiler": " ".join(compiler() or []) or None}


def generate(session: Session, settings: Settings, job: Job, *, force: bool = False, check_eligibility: bool = True,
             compile_fn=None) -> TailoredResume:
    """Generate (or reuse) the tailored resume for ``job``. Always returns a record; ``status`` says what happened.
    Raises TailorError only when the job is not eligible (nothing recorded)."""
    if check_eligibility:
        ok, why = eligible(session, job)
        if not ok:
            raise TailorError(f"Not generated for job #{job.id}: {why}")
    c = cfg(session)
    tr = TailoredResume(job_id=job.id, company_name=job.company_name, job_title=job.title,
                        job_url=job.apply_url or job.source_url, status="pending", inputs_sha256="-")
    try:
        tex, info, master = load_template(session, settings)
        f = facts(session)
        job_hash, inputs = _inputs(info, f, job, c["page_target"])
        tr.source_version_id, tr.template_sha256, tr.facts_sha256 = master.id, info["sha256"], f["sha256"]
        tr.job_sha256, tr.inputs_sha256 = job_hash, inputs
        if not force:
            prev = session.scalar(select(TailoredResume).where(TailoredResume.job_id == job.id,
                                                               TailoredResume.inputs_sha256 == inputs,
                                                               TailoredResume.status == "valid")
                                  .order_by(TailoredResume.id.desc()))
            if prev is not None and file_ok(settings, prev):
                return prev  # unchanged inputs, intact file: reuse
        res = tailor(session, settings, job, tex)
        tr.tailoring_status, tr.decisions = "ok", res["decisions"]
        tr.content_sha256 = _sha(res["tex"])
    except TailorError as exc:
        tr.tailoring_status = tr.tailoring_status or "failed"
        return _fail(session, tr, str(exc))
    session.add(tr)
    session.flush()
    try:
        pdf = (compile_fn or compile_pdf)(res["tex"], int(c["compile_timeout_s"]))
        tr.compile_status = "ok"
    except CompilerUnavailable as exc:
        tr.compile_status = "unavailable"
        return _fail(session, tr, str(exc))
    except TailorError as exc:
        tr.compile_status = "failed"
        return _fail(session, tr, str(exc))
    out_dir = settings.files_dir / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = _sha(pdf)
    name = f"{_slug(job.company_name)}_{job.id}_{tr.content_sha256[:12]}"
    path = out_dir / f"{name}.pdf"
    n = 1
    while path.exists() and _sha(path.read_bytes()) != digest:  # never overwrite a different artifact
        n += 1
        path = out_dir / f"{name}-{n}.pdf"
    if not path.exists():
        path.write_bytes(pdf)
        path.chmod(0o600)
    tr.pdf_path, tr.pdf_sha256 = f"{OUTPUT_DIR}/{path.name}", digest
    excluded = res["excluded_full"]
    v = validate(session, settings, path, expected=res["expected"], header=res["header"], job=job, master=master,
                 excluded_text=excluded, page_target=c["page_target"], corrections=res["corrections"])
    tr.validation, tr.page_count = v, v.get("page_count")
    tr.validation_status = "passed" if v["ok"] else "failed"
    if not v["ok"]:
        return _fail(session, tr, "Validation failed: " + "; ".join(v["errors"])[:800])
    tr.status, tr.validated_at = "valid", utcnow()
    for old in session.scalars(select(TailoredResume).where(TailoredResume.job_id == job.id,
                                                            TailoredResume.status == "valid",
                                                            TailoredResume.id != tr.id)):
        old.status = "superseded"
    audit.record(session, "tailored.generated", "job", job.id, tailored_resume_id=tr.id, pdf_sha256=digest[:16])
    return tr


def _fail(session: Session, tr: TailoredResume, error: str) -> TailoredResume:
    tr.status, tr.error = "failed", error[:2000]
    if tr.id is None:
        session.add(tr)
    session.flush()
    audit.record(session, "tailored.failed", "job", tr.job_id, tailored_resume_id=tr.id, error=error[:200])
    log.info("tailored resume not generated", extra={"job_id": tr.job_id, "error": error[:200]})
    return tr


# ---- attaching ----------------------------------------------------------------------------------------------------

def attachment_for(session: Session, settings: Settings, app: Application) -> tuple[Path, str]:
    """The file to upload for ``app`` (browser helper): its job's valid, current tailored resume when you made one,
    else the application's approved resume (``resumes.verified_file``). Raises ResumeFileError when neither is
    usable."""
    from jobpilot.services import resumes as resume_svc

    base = resume_svc.verified_file(settings, app.resume_version)  # the approved resume must be intact either way
    if not cfg(session)["attach"]:
        return base, "approved resume"
    tr = latest(session, app.job_id, "valid")
    if tr is None:
        return base, "approved resume"
    problem = attach_problem(session, settings, tr, app)
    if problem:
        log.info("tailored resume not attached", extra={"application_id": app.id, "reason": problem})
        return base, f"approved resume (tailored resume not used: {problem})"
    tr.attached_application_id, tr.attached_at = app.id, utcnow()
    audit.record(session, "tailored.attached", "application", app.id, tailored_resume_id=tr.id)
    return settings.files_dir / tr.pdf_path, f"tailored resume #{tr.id}"


def attach_problem(session: Session, settings: Settings, tr: TailoredResume, app: Application) -> str | None:
    if tr.job_id != app.job_id:
        return "it belongs to another job"
    if tr.status != "valid" or tr.validation_status != "passed":
        return "it is not validated"
    if tr.source_version_id != app.resume_version_id:
        return "the application uses a different resume than the one it was made from"
    if not file_ok(settings, tr):
        return "its file is missing or changed"
    info = template_info(session)
    if not info or info["sha256"] != tr.template_sha256:
        return "the template changed since"
    job = app.job
    if job is None or normalize_company(job.company_name) != normalize_company(tr.company_name):
        return "the company doesn't match"
    job_hash, inputs = _inputs(info, facts(session), job, cfg(session)["page_target"])
    if inputs != tr.inputs_sha256:
        return "the job or your confirmed facts changed since; regenerate it on the job's page"
    return None


def summary(tr: TailoredResume | None) -> str:
    if tr is None:
        return "no tailored resume"
    if tr.status == "valid":
        return f"tailored resume ready ({tr.page_count} page(s), validated {tr.validated_at:%d %b %H:%M})"
    return f"tailored resume {tr.status}: {(tr.error or '')[:160]}"


def revalidate(session: Session, settings: Settings, tr: TailoredResume) -> TailoredResume:
    """Check a stored tailored resume again (file intact, still this job's current content, every validation rule)."""
    job = session.get(Job, tr.job_id)
    problems = []
    if not file_ok(settings, tr):
        problems.append("its file is missing or changed")
    try:
        tex, info, master = load_template(session, settings)
        res = tailor(session, settings, job, tex)
        if _sha(res["tex"]) != tr.content_sha256:
            problems.append("the tailored content would be different now (template, facts or job changed); regenerate")
        elif not problems:
            v = validate(session, settings, settings.files_dir / tr.pdf_path, expected=res["expected"],
                         header=res["header"], job=job, master=master,
                         excluded_text=res["excluded_full"], page_target=cfg(session)["page_target"],
                         corrections=res["corrections"])
            tr.validation = v
            problems += v["errors"]
    except TailorError as exc:
        problems.append(str(exc))
    if problems:
        tr.validation_status, tr.status = "failed", "failed"
        tr.error = "Re-validation failed: " + "; ".join(problems)[:1500]
    else:
        tr.validation_status, tr.validated_at = "passed", utcnow()
        if tr.status != "superseded":
            tr.status = "valid"
    audit.record(session, "tailored.revalidated", "job", tr.job_id, tailored_resume_id=tr.id, ok=not problems)
    return tr


def as_dict(tr: TailoredResume) -> dict[str, Any]:
    return {"id": tr.id, "job_id": tr.job_id, "company": tr.company_name, "title": tr.job_title, "job_url": tr.job_url,
            "status": tr.status, "tailoring": tr.tailoring_status, "compile": tr.compile_status,
            "validation": tr.validation_status, "pdf": tr.pdf_path, "pdf_sha256": tr.pdf_sha256,
            "pages": tr.page_count, "source_version_id": tr.source_version_id, "created_at": tr.created_at,
            "validated_at": tr.validated_at, "attached_application_id": tr.attached_application_id,
            "attached_at": tr.attached_at, "error": tr.error}


def latex_source(session: Session, settings: Settings, tr: TailoredResume) -> str:
    """The LaTeX source of a tailored resume. Only the PDF is stored, but tailoring is deterministic: rebuild it from
    the same template, facts and job, and check it is exactly the source that was compiled (content checksum)."""
    job = session.get(Job, tr.job_id)
    if job is None or not tr.content_sha256:
        raise TailorError("This resume's job or source record is gone; create a new resume")
    tex, info, _ = load_template(session, settings)
    if info["sha256"] != tr.template_sha256:
        raise TailorError("Your Master resume changed since this one was made; create a new resume")
    rendered = tailor(session, settings, job, tex)["tex"]
    if _sha(rendered) != tr.content_sha256:
        raise TailorError("The job or your profile changed since this resume was made; create a new resume")
    return rendered
