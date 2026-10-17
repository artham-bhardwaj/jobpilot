"""Deterministic resume extraction and parsing (no network, no LLM).

``extract_document`` turns a stored resume file into text plus the PDF's link targets. ``parse_resume`` splits
that text into sections and structured entries, keeping each entry's original wording and its location (page,
section, bullet) so every imported value can be traced back to the file.

Parsing is conservative: anything it cannot place is reported as a warning, never guessed. Dates keep their
stated precision ("Dec 2025" is a month, "2021" is only a year).
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PARSER_VERSION = "1"

SECTION_ALIASES = {
    "summary": {"summary", "professional summary", "profile", "about", "about me", "objective"},
    "experience": {"experience", "work experience", "professional experience", "employment", "employment history",
                   "work history"},
    "skills": {"technical skills", "skills", "core skills", "key skills", "skills and tools"},
    "projects": {"projects", "personal projects", "selected projects", "key projects"},
    "achievements": {"achievements", "awards", "achievements and awards", "honors", "honours"},
    "certifications": {"certifications", "certificates", "licenses and certifications", "certification"},
    "education": {"education", "academics", "academic background"},
}
_ENTRY = ("•", "●", "■", "*")
_SUB = ("◦", "○", "▪", "-", "–")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE = r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+)?(?:19|20)\d{2}"
_END = rf"(?:{_DATE}|Present|Current|Now|Ongoing)"
DATE_RANGE = re.compile(rf"(?P<start>{_DATE})\s*(?:–|—|-|to)\s*(?P<end>{_END})\s*$", re.I)
SINGLE_DATE = re.compile(rf"(?P<start>{_DATE})\s*$", re.I)
EMAIL = re.compile(r"[^@\s<>·|,;]+@[^@\s<>·|,;]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
URL = re.compile(r"\b(?:https?://|www\.)[^\s<>()·|,]+|\b(?:github\.com|linkedin\.com|gitlab\.com)/[^\s<>()·|,]+", re.I)
_VERSION = re.compile(r"^(?:v)?\d+(?:\.\d+)*(?:\.x)?$", re.I)
_METRIC = re.compile(r"(?:under\s+)?\d[\d,]*(?:\.\d+)?\s*(?:\+|%|ms\b|s\b|seconds\b|x\b)?(?:\s*(?:of|in|across)\s+\d+)?")


class ExtractionError(ValueError):
    pass


# ---- Extraction ------------------------------------------------------------------------------------------

def _pdftotext(data: bytes) -> str | None:
    """Text via poppler's ``pdftotext -layout`` when installed (keeps the spaces around bold runs that pypdf drops)."""
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="jobpilot-pdf-") as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(data)
        try:
            out = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(src), "-"], capture_output=True, timeout=60,
                                 check=True)
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("pdftotext failed; falling back to pypdf", extra={"error": type(exc).__name__})
            return None
    return out.stdout.decode("utf-8", errors="replace")


def _pdf_links(reader) -> list[dict[str, Any]]:
    links = []
    for page_no, page in enumerate(reader.pages, start=1):
        for annot in page.get("/Annots") or []:
            try:
                a = annot.get_object()
                uri = (a.get("/A") or {}).get("/URI")
                rect = [float(x) for x in a.get("/Rect") or [0, 0, 0, 0]]
            except Exception:  # malformed annotation: skip it
                continue
            if isinstance(uri, str) and uri.strip():
                links.append({"page": page_no, "uri": uri.strip(), "x": rect[0], "y": rect[1]})
    # Reading order: page, then top to bottom, then left to right.
    return sorted(links, key=lambda li: (li["page"], -round(li["y"]), li["x"]))


def extract_document(filename: str, data: bytes, *, use_pdftotext: bool = True) -> dict[str, Any]:
    """Text, link targets and page count of a resume file. Raises ``ExtractionError`` if nothing usable."""
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if not data:
        raise ExtractionError("File is empty")
    out: dict[str, Any] = {"text": "", "links": [], "page_count": None, "method": None, "warnings": []}
    if ext == "pdf":
        if not data.startswith(b"%PDF-"):
            raise ExtractionError("File content does not look like a PDF")
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ExtractionError("PDF is password-protected")
            out["page_count"] = len(reader.pages)
            out["links"] = _pdf_links(reader)
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(f"Could not read PDF: {type(exc).__name__}") from exc
        text = _pdftotext(data) if use_pdftotext else None
        if text and text.strip():
            out["text"], out["method"] = text, "pdftotext"
        else:
            out["text"] = "\f".join((p.extract_text() or "") for p in reader.pages)
            out["method"] = "pypdf"
            out["warnings"].append("pdftotext (poppler-utils) not available: used pypdf, which can drop spaces "
                                   "around bold text. Check imported wording.")
    elif ext == "docx":
        from jobpilot.services.resumes import extract_text

        res = extract_text(filename, data)
        if not res["ok"]:
            raise ExtractionError("; ".join(res["errors"]))
        out["text"], out["method"] = res["text"], "docx"
    elif ext in ("txt", "md"):
        try:
            out["text"], out["method"] = data.decode("utf-8"), "text"
        except UnicodeDecodeError as exc:
            raise ExtractionError("Text file is not valid UTF-8") from exc
    else:
        raise ExtractionError(f"Unsupported resume type .{ext}")
    if not out["text"].strip():
        raise ExtractionError("No extractable text (scanned image?)")
    return out


# ---- Small parsers ---------------------------------------------------------------------------------------

def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines, removing end-of-line hyphenation ("enter-" + "prise" → "enterprise")."""
    out = ""
    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue
        if out and re.search(r"[A-Za-z]-$", out) and re.match(r"[a-z]", ln):
            out = out[:-1] + ln
        else:
            out = f"{out} {ln}" if out else ln
    return clean_text(out)


def parse_partial_date(text: str | None) -> dict[str, Any] | None:
    """"Dec 2025" → month precision; "2021" → year precision; "Present" → current."""
    if not text:
        return None
    t = text.strip()
    if re.fullmatch(r"(?i)present|current|now|ongoing", t):
        return {"text": t, "current": True}
    m = re.fullmatch(r"(?i)(?:([a-z]{3})[a-z]*\.?\s+)?((?:19|20)\d{2})", t)
    if not m:
        return None
    month = _MONTHS.get((m.group(1) or "").lower()) if m.group(1) else None
    return {"text": t, "year": int(m.group(2)), "month": month, "precision": "month" if month else "year"}


def to_date(part: dict[str, Any] | None) -> date | None:
    """A date column value only when the month is known (stored as day 1 of that month). Year-only → None."""
    if not part or part.get("current") or not part.get("month"):
        return None
    return date(part["year"], part["month"], 1)


def split_dates(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split a trailing date range ("… Dec 2025 – Present") off a header line."""
    m = DATE_RANGE.search(text)
    if m:
        return text[:m.start()].strip(" ,|·"), {"text": clean_text(m.group(0)), "start": parse_partial_date(m.group("start")),
                                                  "end": parse_partial_date(m.group("end"))}
    m = SINGLE_DATE.search(text)
    if m and m.start() > 0:
        return text[:m.start()].strip(" ,|·"), {"text": m.group(0).strip(), "start": None,
                                                 "end": parse_partial_date(m.group("start"))}
    return text.strip(), None


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` outside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == sep and depth <= 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def parse_skill_item(raw: str) -> list[dict[str, Any]]:
    """"Java (21)" → Java v21; "RAG (TF-IDF, Embeddings)" → RAG + two related skills; "JPA/Hibernate" → two skills."""
    from jobpilot.services.skills_taxonomy import SKILLS, canonical

    raw = clean_text(raw).strip(" .")
    m = re.fullmatch(r"(.+?)\s*\((.+)\)", raw)
    base, inner = (m.group(1).strip(), m.group(2).strip()) if m else (raw, None)
    version, children = None, []
    if inner:
        if _VERSION.match(inner):
            version = inner
        else:
            children = split_top_level(inner)
    vm = re.fullmatch(r"(.+?)\s+(v?\d+(?:\.\d+)*(?:\.x)?)", base)
    if vm and not version and canonical(vm.group(1)) in SKILLS:
        base, version = vm.group(1), vm.group(2)
    names = [base]
    if "/" in base and canonical(base) not in SKILLS:
        pieces = [p.strip() for p in base.split("/")]
        if all(len(p) >= 3 for p in pieces):
            names = pieces
    items = [{"name": n, "version": version if len(names) == 1 else None, "raw": raw, "parent": None} for n in names]
    for c in children:
        items.append({"name": c, "version": None, "raw": raw, "parent": base})
    return items


def metrics_in(text: str) -> list[str]:
    """Numbers stated in a line ("50%", "15+", "under 200ms"): claims to be able to back up."""
    return [clean_text(m.group(0)) for m in _METRIC.finditer(text) if re.search(r"\d", m.group(0))
            and not re.fullmatch(r"(?:19|20)\d{2}", m.group(0).strip())]


def _section_of(line: str) -> str | None:
    t = re.sub(r"[^a-z& ]", "", line.strip().lower().rstrip(":")).replace("&", "and")
    t = clean_text(t)
    if not t or len(t.split()) > 4:
        return None
    for key, names in SECTION_ALIASES.items():
        if t in names:
            return key
    return None


def _marker(line: str) -> tuple[str | None, str]:
    s = line.strip()
    for m in _ENTRY:
        if s.startswith(m) and (len(s) == len(m) or s[len(m)] == " " or m in "•●■"):
            return "entry", s[len(m):].strip()
    for m in _SUB:
        if s.startswith(m) and (len(s) > len(m) and (s[len(m)] == " " or m in "◦○▪")):
            return "sub", s[len(m):].strip()
    return None, s


def _columns(line: str) -> list[str]:
    """Layout text separates right-aligned columns (dates, link labels) with runs of spaces."""
    return [c.strip() for c in re.split(r"\s{3,}", line.strip()) if c.strip()]


def _entries(lines: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """Group a section's lines into entries: header, extra lines before the first bullet, and bullets."""
    entries: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for page, line in lines:
        kind, body = _marker(line)
        if kind == "entry" or cur is None:
            cur = {"header": body, "header_raw": line.strip(), "page": page, "extra": [], "bullets": []}
            entries.append(cur)
        elif kind == "sub":
            cur["bullets"].append({"lines": [body], "page": page})
        elif cur["bullets"]:
            cur["bullets"][-1]["lines"].append(body)
        else:
            cur["extra"].append(body)
    return entries


def _bullets(entry: dict[str, Any], where: str) -> list[dict[str, Any]]:
    out = []
    for n, b in enumerate(entry["bullets"], start=1):
        text = join_lines(b["lines"])
        out.append({"text": text, "original": "\n".join(ln.strip() for ln in b["lines"]),
                    "location": f"page {b['page']} · {where} · bullet {n}", "metrics": metrics_in(text)})
    return out


# ---- Section parsers -------------------------------------------------------------------------------------

def _split_company(text: str) -> tuple[str, str, str | None]:
    """"System Engineer, Tata Consultancy Services (TCS)" → (title, company, alias)."""
    m = re.match(r"(.+?)\s+at\s+(.+)$", text) or re.match(r"(.+?),\s*(.+)$", text) or \
        re.match(r"(.+?)\s+[–—|]\s+(.+)$", text)
    title, company = (m.group(1), m.group(2)) if m else (text, "")
    alias = None
    am = re.search(r"\(([A-Za-z&.]{2,10})\)\s*$", company)
    if am:
        alias, company = am.group(1), company[:am.start()].strip()
    return clean_text(title), clean_text(company), alias


def _parse_experience(lines) -> list[dict[str, Any]]:
    out = []
    for i, e in enumerate(_entries(lines)):
        cols = _columns(e["header"])
        head, dates = split_dates(" ".join(cols) if len(cols) > 1 else e["header"])
        title, company, alias = _split_company(head)
        where = f"EXPERIENCE · {company or title}"
        out.append({"title": title, "company": company, "company_alias": alias, "dates": dates,
                    "original": e["header_raw"], "location": f"page {e['page']} · {where} · header",
                    "extra": [join_lines(e["extra"])] if e["extra"] else [],
                    "bullets": _bullets(e, where), "index": i})
    return out


def _parse_projects(lines) -> list[dict[str, Any]]:
    out = []
    for i, e in enumerate(_entries(lines)):
        cols = _columns(e["header"])
        head, dates = split_dates(" ".join(cols) if len(cols) > 1 else e["header"])
        m = re.match(r"(.+?)\s+[–—:|-]\s+(.+)$", head)
        name, tagline = (clean_text(m.group(1)), clean_text(m.group(2))) if m else (clean_text(head), None)
        stack, link_label = [], None
        if e["extra"]:
            first = e["extra"][0]
            scols = _columns(first)
            if len(scols) > 1 and len(scols[-1].split()) <= 2 and "," not in scols[-1]:
                link_label = scols[-1]
                first = " ".join(scols[:-1])
            else:
                lm = re.search(r"\s(GitHub|Link|Demo|Live|Code)\s*$", first)
                if lm:
                    link_label, first = lm.group(1), first[:lm.start()]
            stack = split_top_level(join_lines([first] + e["extra"][1:]))
        where = f"PROJECTS · {name}"
        out.append({"name": name, "tagline": tagline, "dates": dates, "technologies": stack, "link_label": link_label,
                    "original": "\n".join([e["header_raw"]] + e["extra"]), "location": f"page {e['page']} · {where} · header",
                    "bullets": _bullets(e, where), "index": i, "url": None})
    return out


def _parse_education(lines) -> list[dict[str, Any]]:
    out = []
    for i, e in enumerate(_entries(lines)):
        cols = _columns(e["header"])
        head, dates = split_dates(" ".join(cols) if len(cols) > 1 else e["header"])
        parts = re.split(r"\s+[—–|]\s+|\s+-\s+|\s*—\s*", head, maxsplit=1)
        left, right = (parts[0], parts[1]) if len(parts) == 2 else (head, "")
        pm = re.fullmatch(r"(.+?)\s*\((.+)\)", left.strip())
        if pm:
            degree, field = pm.group(1), pm.group(2)
        elif "," in left:
            degree, field = [s.strip() for s in left.split(",", 1)]
        else:
            degree, field = left, None
        institution, place = (right.rsplit(",", 1) + [None])[:2] if "," in right else (right, None)
        grade = None
        for x in e["extra"] + [b for bb in e["bullets"] for b in bb["lines"]]:
            gm = re.search(r"\b(CGPA|GPA|CPI|SGPA|Score|Percentage|Grade|Marks)\s*[:\-]\s*([\d.]+\s*(?:/\s*[\d.]+|%)?)", x, re.I)
            if gm:
                grade = f"{gm.group(1)}: {gm.group(2).replace(' ', '')}"
                break
        out.append({"degree": clean_text(degree), "field_of_study": clean_text(field) if field else None,
                    "institution": clean_text(institution), "place": clean_text(place) if place else None,
                    "dates": dates, "grade": grade, "original": "\n".join([e["header_raw"]] + e["extra"]),
                    "location": f"page {e['page']} · EDUCATION · entry {i + 1}", "index": i})
    return out


def _parse_skills(lines) -> list[dict[str, Any]]:
    out = []
    for e in _entries(lines):
        text = join_lines([e["header"]] + e["extra"] + [ln for b in e["bullets"] for ln in b["lines"]])
        if ":" in text:
            category, rest = [s.strip() for s in text.split(":", 1)]
        else:
            category, rest = None, text
        items = [it for raw in split_top_level(rest) for it in parse_skill_item(raw)]
        out.append({"category": category, "items": items, "original": "\n".join([e["header_raw"]] + e["extra"]),
                    "location": f"page {e['page']} · TECHNICAL SKILLS · {category or 'list'}"})
    return out


def _parse_simple(lines, section: str) -> list[dict[str, Any]]:
    out = []
    for i, e in enumerate(_entries(lines)):
        text = join_lines([e["header"]] + e["extra"] + [ln for b in e["bullets"] for ln in b["lines"]])
        if text:
            out.append({"text": text.rstrip("."), "original": "\n".join([e["header_raw"]] + e["extra"]),
                        "location": f"page {e['page']} · {section.upper()} · entry {i + 1}", "metrics": metrics_in(text),
                        "index": i})
    return out


def _parse_header(lines: list[tuple[int, str]]) -> dict[str, Any]:
    texts = [clean_text(t) for _, t in lines if t.strip()]
    out: dict[str, Any] = {"name": None, "headline": None, "email": None, "phone": None, "contact_labels": [],
                           "inline_urls": [], "original": "\n".join(texts)}
    for t in texts:
        if out["name"] is None and not EMAIL.search(t) and not PHONE.search(t):
            out["name"] = t
            continue
        if EMAIL.search(t) or PHONE.search(t) or "·" in t or "|" in t:
            for tok in re.split(r"\s*[·|•]\s*|\s{2,}", t):
                tok = tok.strip()
                if not tok:
                    continue
                if EMAIL.fullmatch(tok) or (EMAIL.search(tok) and not out["email"]):
                    out["email"] = out["email"] or EMAIL.search(tok).group(0)
                elif PHONE.fullmatch(tok):
                    out["phone"] = out["phone"] or tok
                elif URL.search(tok):
                    out["inline_urls"].append(URL.search(tok).group(0))
                else:
                    out["contact_labels"].append(tok)
        elif out["headline"] is None and out["name"] is not None:
            out["headline"] = t
    return out


def _label_for(uri: str) -> str:
    host = re.sub(r"^https?://(www\.)?", "", uri.lower()).split("/")[0]
    if "linkedin.com" in host:
        return "LinkedIn"
    if "github.com" in host:
        return "GitHub"
    if "gitlab.com" in host:
        return "GitLab"
    return "Portfolio"


def _assign_links(parsed: dict[str, Any], links: list[dict[str, Any]]) -> None:
    """Match the PDF's link targets to the contact labels and to projects' link labels, in reading order."""
    web = [li for li in links if not li["uri"].lower().startswith("mailto:")]
    labels = parsed["header"]["contact_labels"]
    contact, rest = web[:len(labels)], web[len(labels):]
    for label, li in zip(labels, contact, strict=False):
        expected = _label_for(li["uri"])
        consistent = label.lower() == expected.lower() or (expected == "Portfolio" and label.lower() not in
                                                            ("linkedin", "github", "gitlab"))
        parsed["links"].append({"label": label, "url": li["uri"], "location": f"page {li['page']} · header link",
                                "consistent": consistent})
    for url in parsed["header"]["inline_urls"]:
        full = url if url.lower().startswith("http") else f"https://{url}"
        parsed["links"].append({"label": _label_for(full), "url": full, "location": "header text", "consistent": True})
    linked = [p for p in parsed["projects"] if p["link_label"]]
    if linked and len(rest) == len(linked):
        for p, li in zip(linked, rest, strict=True):
            p["url"] = li["uri"]
            p["url_location"] = f"page {li['page']} · link '{p['link_label']}'"
    elif linked or rest:
        parsed["warnings"].append(f"{len(rest)} link(s) in the body but {len(linked)} project link label(s): "
                                  "project links not assigned")


def parse_resume(text: str, links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Structured view of a resume's text. Never raises on unusual structure: it reports warnings instead."""
    pages = (text or "").split("\f")
    numbered = [(pn, ln.rstrip()) for pn, page in enumerate(pages, start=1) for ln in page.splitlines() if ln.strip()]
    sections: dict[str, list[tuple[int, str]]] = {"header": []}
    order: list[str] = []
    current = "header"
    warnings: list[str] = []
    for pn, ln in numbered:
        key = _section_of(ln)
        if key:
            current = key
            if key in sections:
                warnings.append(f"Section {key} appears more than once; entries were combined")
            sections.setdefault(key, [])
            order.append(key)
            continue
        if current == "header" and re.fullmatch(r"[A-Z][A-Z &/]{3,40}", ln.strip()):
            warnings.append(f"Unrecognised section '{ln.strip()}' ignored")
            current = "_ignored"
        sections.setdefault(current, []).append((pn, ln))
    parsed: dict[str, Any] = {
        "parser_version": PARSER_VERSION, "sections_found": order, "warnings": warnings, "links": [],
        "header": _parse_header(sections.get("header", [])),
        "summary": join_lines([t for _, t in sections.get("summary", [])]) or None,
        "employment": _parse_experience(sections.get("experience", [])),
        "projects": _parse_projects(sections.get("projects", [])),
        "education": _parse_education(sections.get("education", [])),
        "skills": _parse_skills(sections.get("skills", [])),
        "certifications": _parse_simple(sections.get("certifications", []), "certifications"),
        "achievements": _parse_simple(sections.get("achievements", []), "achievements"),
    }
    for p in parsed["achievements"]:
        m = re.search(r"\bacross\s+(.+)$", p["text"])
        p["platforms"] = [s.strip() for s in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(1)) if s.strip()] if m else []
    _assign_links(parsed, links or [])
    if not parsed["header"]["name"]:
        warnings.append("No name found at the top of the resume")
    for key in ("experience", "education", "skills"):
        if key not in order:
            warnings.append(f"No {key} section found")
    return parsed
