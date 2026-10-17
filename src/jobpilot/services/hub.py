"""Personal information hub: import a resume into the profile, with provenance, and one view over everything.

Pipeline (``import_resume``): read the file → extract text and links → parse → validate → match each value to the
existing profile fact → persist through the profile services → read back and verify → report.

Rules:
- The profile tables stay the single source of truth. The hub adds ``ProfileClaim`` rows saying where each value
  came from, and never keeps a second copy of the profile.
- Imported values are *claims*: new facts are created ``unverified``. Nothing is confirmed by an import.
- A confirmed fact is never changed by an import. A different value becomes a **conflict**; a value for an empty
  field of a confirmed fact becomes a **suggestion**. You apply or dismiss them in the hub.
- An unverified fact is updated only where it still holds the value an earlier import wrote; if you edited it, your
  value is kept and the difference is reported as a conflict.
- Re-importing the same file is idempotent: claims are keyed by (file hash, position), facts are matched by name.
- Everything runs in one transaction (savepoint); a failure leaves the profile unchanged and records the error.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import (Achievement, AnswerBankEntry, AuditLog, CandidateProfile, Certification, Education,
                             Employment, LearningItem, ProfileClaim, ProfileImport, ProfileLink, Project, Resume,
                             ResumeVersion, Skill, utcnow)
from jobpilot.services import audit, profile as profile_svc, resume_parser, skills_taxonomy, storage
from jobpilot.services.text_utils import normalize, normalize_company

SECTION_KINDS = ("links", "employment", "education", "projects", "skills", "certifications", "achievements")
OPEN_OUTCOMES = ("conflict", "suggestion")
_BOARDS = {"cbse", "icse", "isc", "ib", "state board", "nios"}


class HubImportError(ValueError):
    pass


def _key(text: Any) -> str:
    """Comparison key: case, accents, punctuation, whitespace and hyphenation ignored."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def _same(a: Any, b: Any) -> bool:
    return _key(a) == _key(b)


def _same_phone(a: Any, b: Any) -> bool:
    da, db = re.sub(r"\D", "", str(a or "")), re.sub(r"\D", "", str(b or ""))
    if len(da) >= 10 and len(db) >= 10:
        return da[-10:] == db[-10:]
    return da == db


def _same_url(a: Any, b: Any) -> bool:
    def n(u: Any) -> str:
        return re.sub(r"^(https?://)?(www\.)?", "", str(u or "").strip().lower()).rstrip("/")
    return n(a) == n(b)


def _same_month(existing: date | None, part: dict | None) -> bool:
    if not part or part.get("current"):
        return existing is None
    if existing is None:
        return False
    return existing.year == part["year"] and (part.get("month") is None or existing.month == part["month"])


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == []


def _company_match(existing: str, company: str, alias: str | None) -> bool:
    a, b = normalize_company(existing), normalize_company(company)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    initials = "".join(w[0] for w in a.split() if w)
    return bool(alias) and (initials == alias.lower() or a == alias.lower())


def _date_text(d: dict | None) -> str | None:
    return d.get("text") if d else None


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


# ---- The import run -------------------------------------------------------------------------------------

class _Run:
    def __init__(self, session: Session, imp: ProfileImport, source_ref: str, label: str):
        self.s = session
        self.imp = imp
        self.source_ref = source_ref
        self.label = label  # human-readable source, also stored in each created fact's ``source``
        self.items: list[dict[str, Any]] = []
        self.now = utcnow()
        self.prev = {c.claim_key: c for c in session.scalars(select(ProfileClaim)
                                                              .where(ProfileClaim.source_ref == source_ref))}
        # The latest value any resume import wrote for each key (keys are name-based, so they carry across versions).
        self.written = {c.claim_key: c.value for c in session.scalars(
            select(ProfileClaim).where(ProfileClaim.source_kind == "resume",
                                       ProfileClaim.outcome.in_(("created", "updated", "applied")))
            .order_by(ProfileClaim.id))}

    # -- claims and report

    def claim(self, key: str, *, category: str, entity_type: str | None, entity_id: int | None, field: str | None,
              value: Any, original: str | None, location: str | None, outcome: str, existing: Any = None,
              notes: str | None = None, related: dict | None = None, confidence: float = 1.0,
              state: str = "resume_claim", source_kind: str = "resume", label: str | None = None) -> ProfileClaim:
        c = self.prev.get(key)
        if c is None:
            c = ProfileClaim(source_ref=self.source_ref, claim_key=key[:200])
            self.s.add(c)
            self.prev[key] = c
        # A conflict or suggestion you dismissed stays dismissed while the value is unchanged.
        if c.outcome == "dismissed" and outcome in OPEN_OUTCOMES and _same(c.value, value):
            outcome = "dismissed"
        elif c.outcome == "applied" and outcome == "unchanged":
            outcome = "applied"
        c.import_id = self.imp.id
        c.category, c.entity_type, c.entity_id, c.field = category, entity_type, entity_id, field
        c.value, c.original_text = _str(value), original
        c.existing_value = _str(existing) if outcome in OPEN_OUTCOMES else None
        c.source_kind, c.source_label, c.source_location = source_kind, (label or self.label)[:300], (location or "")[:300]
        c.state = "rejected" if outcome == "dismissed" else (c.state if c.state in ("rejected", "verified") and
                                                              outcome == "applied" else state)
        c.outcome, c.confidence, c.notes, c.related = outcome, confidence, notes, related
        c.extracted_at = self.now
        self.items.append({"key": key, "category": category, "field": field, "outcome": outcome,
                           "value": (_str(value) or "")[:160], "existing": (_str(existing) or "")[:160] if
                           outcome in OPEN_OUTCOMES else None, "entity_type": entity_type, "entity_id": entity_id,
                           "notes": notes})
        return c

    def prev_value(self, key: str) -> str | None:
        """The value an earlier resume import wrote for this claim (this file first, else the latest import)."""
        c = self.prev.get(key)
        if c is not None and c.outcome in ("created", "updated", "applied"):
            return c.value
        return self.written.get(key)

    # -- writes go through the profile services (validation + audit)

    def save(self, kind: str, obj: Any | None, changes: dict[str, Any]):
        model, schema = profile_svc.FACT_MODELS[kind]
        values = {f: getattr(obj, f) for f in schema.model_fields if hasattr(obj, f)} if obj is not None else {}
        values.update(changes)
        if obj is None:
            values.setdefault("status", "unverified")
            values.setdefault("source", self.label[:200])
        return profile_svc.save_fact(self.s, kind, schema.model_validate(values), obj.id if obj is not None else None)

    def merge(self, obj: Any, field: str, new: Any, key: str, changes: dict, *, equal=_same,
              confirmed: bool | None = None) -> tuple[str, Any]:
        """Decide what to do with one field of an existing fact. Returns (outcome, existing value)."""
        cur = getattr(obj, field)
        confirmed = (getattr(obj, "status", None) == "confirmed") if confirmed is None else confirmed
        if _empty(new):
            return "unchanged", cur
        if _empty(cur):
            if confirmed:
                return "suggestion", cur
            changes[field] = new
            return "updated", cur
        if equal(cur, new):
            return "unchanged", cur
        prev = self.prev_value(key)
        if not confirmed and prev is not None and equal(cur, prev):
            changes[field] = new  # still the value an earlier import wrote: safe to refresh
            return "updated", cur
        return "conflict", cur


def _source_label(filename: str, sha: str) -> str:
    return f"resume {filename} (sha256 {sha[:12]})"


def _register_version(run: _Run, settings: Settings, filename: str, data: bytes, sha: str) -> ResumeVersion:
    from jobpilot.services import resumes as resume_svc

    existing = run.s.scalar(select(ResumeVersion).where(ResumeVersion.sha256 == sha).order_by(ResumeVersion.id))
    if existing is not None:
        run.claim("resume_version", category="resume", entity_type="resume_version", entity_id=existing.id,
                  field="file", value=existing.label, original=existing.original_filename, location="file",
                  outcome="unchanged", notes=f"already registered as {existing.label} ({existing.status}); "
                                             "the file was not stored again", related={"sha256": sha})
        return existing
    master = run.s.scalar(select(Resume).where(Resume.is_master.is_(True)).order_by(Resume.id)) or \
        resume_svc.create_resume(run.s, "Master resume", "master", True)
    version = resume_svc.import_version(run.s, settings, master, filename, data,
                                        note=f"uploaded file {Path(filename).name} (information hub import)",
                                        change_summary="Imported by the information hub; review and approve it")
    run.claim("resume_version", category="resume", entity_type="resume_version", entity_id=version.id, field="file",
              value=version.label, original=version.original_filename, location="file", outcome="created",
              notes="registered as a draft version: approving it is your decision", related={"sha256": sha})
    return version


def _import_profile(run: _Run, parsed: dict) -> None:
    h = parsed["header"]
    profile = profile_svc.get_profile(run.s)
    if profile.id is None:
        profile = profile_svc._profile_for_write(run.s)
    confirmed = profile.confirmed_at is not None
    name = h.get("name")
    first = last = None
    if name and len(name.split()) >= 2:
        first, last = name.split()[0], name.split()[-1]
    fields = [("full_name", name, _same), ("first_name", first, _same), ("last_name", last, _same),
              ("headline", h.get("headline"), _same), ("email", h.get("email"), lambda a, b: str(a).lower() == str(b).lower()),
              ("phone", h.get("phone"), _same_phone), ("summary", parsed.get("summary"), _same)]
    changes: dict[str, Any] = {}
    for field, value, eq in fields:
        if _empty(value):
            continue
        key = f"profile.{field}"
        outcome, cur = run.merge(profile, field, value, key, changes, equal=eq, confirmed=confirmed)
        run.claim(key, category="contact" if field in ("email", "phone") else "profile", entity_type="profile",
                  entity_id=profile.id, field=field, value=value, original=h.get("original") if field != "summary" else value,
                  location="page 1 · header" if field != "summary" else "SUMMARY", outcome=outcome, existing=cur,
                  notes="derived from the full name" if field in ("first_name", "last_name") else None,
                  confidence=0.9 if field in ("first_name", "last_name") else 1.0)
    if changes:
        data = {f: getattr(profile, f) for f in profile_svc.ProfileIn.model_fields}
        data.update(changes)
        profile_svc.update_profile(run.s, profile_svc.ProfileIn.model_validate(data))


def _import_links(run: _Run, parsed: dict) -> None:
    links = profile_svc.list_facts(run.s, "links")
    for li in parsed["links"]:
        key = f"link.{_key(li['label'])}"
        match = next((x for x in links if _same_url(x.url, li["url"])), None) or \
            next((x for x in links if _same(x.label, li["label"])), None)
        conf = 1.0 if li.get("consistent", True) else 0.5
        notes = None if li.get("consistent", True) else "the link target does not look like its label"
        if match is None:
            obj = run.save("links", None, {"label": li["label"], "url": li["url"], "status": "unverified"})
            links.append(obj)
            outcome, cur, eid = "created", None, obj.id
        else:
            changes: dict = {}
            outcome, cur = run.merge(match, "url", li["url"], key, changes, equal=_same_url)
            if changes:
                run.save("links", match, changes)
            eid = match.id
        run.claim(key, category="link", entity_type="links", entity_id=eid, field="url", value=li["url"],
                  original=li["label"], location=li["location"], outcome=outcome, existing=cur, notes=notes,
                  confidence=conf)


def _import_employment(run: _Run, parsed: dict) -> dict[int, int]:
    """Returns {resume entry index: employment id}."""
    ids: dict[int, int] = {}
    rows = list(run.s.scalars(select(Employment).order_by(Employment.id)))
    for e in parsed["employment"]:
        base = f"employment.{_key(e['company'] or e['title'])[:40]}"
        start, end = (e["dates"] or {}).get("start"), (e["dates"] or {}).get("end")
        bullets = [b["text"] for b in e["bullets"]]
        cands = [r for r in rows if _company_match(r.company, e["company"], e["company_alias"])]
        match = next((r for r in cands if _same(r.title, e["title"])), None) or (cands[0] if cands else None)
        header_related = {"company_alias": e["company_alias"], "dates": _date_text(e["dates"]),
                          "date_precision": (start or {}).get("precision")}
        if match is None:
            obj = run.save("employment", None, {
                "company": e["company"] or "(company not stated)", "title": e["title"],
                "start_date": resume_parser.to_date(start), "end_date": resume_parser.to_date(end),
                "description": "\n".join(bullets) or None})
            rows.append(obj)
            ids[e["index"]] = obj.id
            for field, value in (("company", e["company"]), ("title", e["title"]), ("dates", _date_text(e["dates"]))):
                run.claim(f"{base}.{field}", category="employment", entity_type="employment", entity_id=obj.id,
                          field=field, value=value, original=e["original"], location=e["location"], outcome="created",
                          related=header_related, notes="month precision: stored as the 1st of the month"
                          if field == "dates" and (start or {}).get("precision") == "month" else None)
            for j, b in enumerate(e["bullets"]):
                run.claim(f"{base}.bullet[{j}]", category="employment", entity_type="employment", entity_id=obj.id,
                          field="description", value=b["text"], original=b["original"], location=b["location"],
                          outcome="created", related={"metrics": b["metrics"]})
            continue
        ids[e["index"]] = match.id
        changes: dict[str, Any] = {}
        for field, value in (("company", e["company"]), ("title", e["title"])):
            eq = (lambda a, b: _company_match(a, b, e["company_alias"])) if field == "company" else _same
            outcome, cur = run.merge(match, field, value, f"{base}.{field}", changes, equal=eq)
            run.claim(f"{base}.{field}", category="employment", entity_type="employment", entity_id=match.id,
                      field=field, value=value, original=e["original"], location=e["location"], outcome=outcome,
                      existing=cur, related=header_related)
        # Dates: compared at the precision the resume states.
        ok = _same_month(match.start_date, start) and _same_month(match.end_date, end)
        if ok:
            d_out, d_cur = "unchanged", None
        elif match.status == "confirmed":
            d_out, d_cur = "conflict", f"{match.start_date or '?'} → {match.end_date or 'present'}"
        elif match.start_date is None and match.end_date is None:
            changes.update(start_date=resume_parser.to_date(start), end_date=resume_parser.to_date(end))
            d_out, d_cur = "updated", None
        else:
            d_out, d_cur = "conflict", f"{match.start_date or '?'} → {match.end_date or 'present'}"
        run.claim(f"{base}.dates", category="employment", entity_type="employment", entity_id=match.id, field="dates",
                  value=_date_text(e["dates"]), original=e["original"], location=e["location"], outcome=d_out,
                  existing=d_cur, related=header_related)
        desc = changes.get("description", match.description) or ""
        for j, b in enumerate(e["bullets"]):
            key = f"{base}.bullet[{j}]"
            if _key(b["text"]) in _key(desc):
                outcome = "unchanged"
            elif match.status == "confirmed" or (run.prev_value(key) is not None):
                # Confirmed text is yours; a bullet an earlier import added and you since removed stays removed.
                outcome = "suggestion"
            else:
                desc = f"{desc}\n{b['text']}".strip()
                changes["description"] = desc
                outcome = "updated"
            run.claim(key, category="employment", entity_type="employment", entity_id=match.id, field="description",
                      value=b["text"], original=b["original"], location=b["location"], outcome=outcome,
                      related={"metrics": b["metrics"]},
                      notes="not in the role's description" if outcome == "suggestion" else None)
        if changes:
            run.save("employment", match, changes)
    return ids


def _edu_notes(ed: dict, existing: Education | None = None) -> list[str]:
    notes = []
    if ed.get("place"):
        notes.append(f"Location: {ed['place']}")
    d = ed.get("dates") or {}
    has_dates = existing is not None and (existing.start_date or existing.end_date)
    if d and not has_dates and not any((p or {}).get("month") for p in (d.get("start"), d.get("end"))):
        notes.append(f"Dates on resume: {d['text']} (year only)")
    if (ed.get("institution") or "").lower() in _BOARDS:
        notes.append(f"{ed['institution']} is the examination board as stated; the school is not named on the resume")
    return notes


def _import_education(run: _Run, parsed: dict) -> None:
    rows = list(run.s.scalars(select(Education).order_by(Education.id)))
    for ed in parsed["education"]:
        base = f"education.{_key(ed['degree'])[:30]}.{_key(ed['institution'])[:30]}"
        inst = normalize(ed["institution"])
        match = next((r for r in rows if inst and (inst in normalize(r.institution) or normalize(r.institution) in inst)
                      and (not r.degree or _same(r.degree, ed["degree"]))), None)
        d = ed.get("dates") or {}
        years = " – ".join(str(p["year"]) for p in (d.get("start"), d.get("end")) if p and p.get("year")) or None
        if match is None:
            obj = run.save("education", None, {
                "institution": ed["institution"] or "(not stated)", "degree": ed["degree"],
                "field_of_study": ed["field_of_study"], "grade": ed["grade"],
                "start_date": resume_parser.to_date(d.get("start")), "end_date": resume_parser.to_date(d.get("end")),
                "notes": "\n".join(_edu_notes(ed)) or None})
            rows.append(obj)
            for field, value in (("institution", ed["institution"]), ("degree", ed["degree"]),
                                 ("field_of_study", ed["field_of_study"]), ("grade", ed["grade"]), ("dates", years)):
                if not _empty(value):
                    run.claim(f"{base}.{field}", category="education", entity_type="education", entity_id=obj.id,
                              field=field, value=value, original=ed["original"], location=ed["location"],
                              outcome="created")
            for note in _edu_notes(ed):
                run.claim(f"{base}.notes.{_key(note)[:40]}", category="education", entity_type="education",
                          entity_id=obj.id, field="notes", value=note, original=ed["original"], location=ed["location"],
                          outcome="created")
            continue
        changes: dict[str, Any] = {}
        for field, value in (("degree", ed["degree"]), ("field_of_study", ed["field_of_study"]), ("grade", ed["grade"])):
            if _empty(value):
                continue
            outcome, cur = run.merge(match, field, value, f"{base}.{field}", changes)
            run.claim(f"{base}.{field}", category="education", entity_type="education", entity_id=match.id, field=field,
                      value=value, original=ed["original"], location=ed["location"], outcome=outcome, existing=cur)
        run.claim(f"{base}.institution", category="education", entity_type="education", entity_id=match.id,
                  field="institution", value=ed["institution"], original=ed["original"], location=ed["location"],
                  outcome="unchanged")
        if years:
            if match.start_date is None and match.end_date is None:
                ok = True  # year-only dates are kept in the notes (see below), never as invented exact dates
            else:
                ok = all(p is None or not p.get("year") or (cur is not None and cur.year == p["year"])
                         for p, cur in ((d.get("start"), match.start_date), (d.get("end"), match.end_date)))
            run.claim(f"{base}.dates", category="education", entity_type="education", entity_id=match.id, field="dates",
                      value=years, original=ed["original"], location=ed["location"],
                      outcome="unchanged" if ok else "conflict",
                      existing=None if ok else f"{match.start_date or '?'} → {match.end_date or '?'}")
        for note in _edu_notes(ed, match):
            key = f"{base}.notes.{_key(note)[:40]}"
            if _key(note) in _key(match.notes) or (note.startswith("Location:") and _key(ed.get("place")) in
                                                   _key(f"{match.institution} {match.notes or ''}")):
                outcome = "unchanged"
            elif match.status == "confirmed" or run.prev_value(key) is not None:
                outcome = "suggestion"
            else:
                changes["notes"] = "\n".join(x for x in ((changes.get("notes") or match.notes), note) if x)
                outcome = "updated"
            run.claim(key, category="education", entity_type="education", entity_id=match.id, field="notes",
                      value=note, original=ed["original"], location=ed["location"], outcome=outcome)
        if changes:
            run.save("education", match, changes)


def _project_description(p: dict) -> str:
    return "\n".join(([p["tagline"]] if p.get("tagline") else []) + [b["text"] for b in p["bullets"]])


def _import_projects(run: _Run, parsed: dict) -> dict[int, int]:
    ids: dict[int, int] = {}
    rows = list(run.s.scalars(select(Project).order_by(Project.id)))
    for p in parsed["projects"]:
        base = f"project.{_key(p['name'])[:50]}"
        d = p.get("dates") or {}
        values = {"name": p["name"], "description": _project_description(p), "technologies": p["technologies"] or None,
                  "url": p.get("url"), "start_date": resume_parser.to_date(d.get("start")),
                  "end_date": resume_parser.to_date(d.get("end"))}
        match = next((r for r in rows if _same(r.name, p["name"])), None)
        related = {"tagline": p.get("tagline"), "dates": _date_text(d), "link_location": p.get("url_location"),
                   "kind_basis": "listed under PROJECTS, separate from employment"}
        if match is None:
            obj = run.save("projects", None, dict(values, kind="personal"))
            rows.append(obj)
            ids[p["index"]] = obj.id
            for field in ("name", "description", "technologies", "url", "dates"):
                value = _date_text(d) if field == "dates" else values[field]
                if not _empty(value):
                    run.claim(f"{base}.{field}", category="project", entity_type="projects", entity_id=obj.id,
                              field=field, value=value, original=p["original"], location=p.get("url_location") if
                              field == "url" else p["location"], outcome="created", related=related,
                              notes="kind recorded as 'personal project' (never work experience); change it if it was "
                                    "academic or open source" if field == "name" else None)
        else:
            ids[p["index"]] = match.id
            changes: dict[str, Any] = {}
            run.claim(f"{base}.name", category="project", entity_type="projects", entity_id=match.id, field="name",
                      value=p["name"], original=p["original"], location=p["location"], outcome="unchanged",
                      related=related)
            for field in ("description", "technologies", "url"):
                eq = _same_url if field == "url" else _same
                outcome, cur = run.merge(match, field, values[field], f"{base}.{field}", changes, equal=eq)
                run.claim(f"{base}.{field}", category="project", entity_type="projects", entity_id=match.id,
                          field=field, value=values[field], original=p["original"], location=p["location"],
                          outcome=outcome, existing=cur, related=related)
            ok = _same_month(match.start_date, d.get("start")) and _same_month(match.end_date, d.get("end"))
            if not ok and match.status != "confirmed" and match.start_date is None and match.end_date is None:
                changes.update(start_date=values["start_date"], end_date=values["end_date"])
                outcome = "updated"
            else:
                outcome = "unchanged" if ok else "conflict"
            run.claim(f"{base}.dates", category="project", entity_type="projects", entity_id=match.id, field="dates",
                      value=_date_text(d), original=p["original"], location=p["location"], outcome=outcome,
                      existing=None if ok else f"{match.start_date or '?'} → {match.end_date or 'present'}")
            if changes:
                run.save("projects", match, changes)
        pid = ids[p["index"]]
        for j, b in enumerate(p["bullets"]):
            obj = run.s.get(Project, pid)
            present = _key(b["text"]) in _key(obj.description)
            run.claim(f"{base}.bullet[{j}]", category="project", entity_type="projects", entity_id=pid,
                      field="description", value=b["text"], original=b["original"], location=b["location"],
                      outcome=("created" if match is None else "unchanged") if present else "suggestion",
                      related={"metrics": b["metrics"]})
    return ids


def _skill_registry(parsed: dict, emp_ids: dict[int, int], proj_ids: dict[int, int]) -> dict[str, dict]:
    """Canonical skill → everything the resume says about it."""
    canonical = skills_taxonomy.canonical
    reg: dict[str, dict] = {}

    def entry(name: str) -> dict:
        c = canonical(name)
        return reg.setdefault(c, {"canonical": c, "display": name, "aliases": [], "versions": [], "resume_categories": [],
                                  "parents": [], "employment": [], "projects": [], "listed": False, "location": None,
                                  "original": None})

    for group in parsed["skills"]:
        for it in group["items"]:
            r = entry(it["name"])
            r["listed"] = True
            r["location"] = r["location"] or group["location"]
            r["original"] = r["original"] or group["original"]
            if it["name"] not in r["aliases"]:
                r["aliases"].append(it["name"])
            if group["category"] and group["category"] not in r["resume_categories"]:
                r["resume_categories"].append(group["category"])
            if it["version"]:
                r["versions"].append({"version": it["version"], "where": "TECHNICAL SKILLS"})
            if it["parent"] and it["parent"] not in r["parents"]:
                r["parents"].append(it["parent"])
    for p in parsed["projects"]:
        pid = proj_ids.get(p["index"])
        for tech in p["technologies"]:
            for it in resume_parser.parse_skill_item(tech):
                c = canonical(it["name"])
                if c in reg or c in skills_taxonomy.SKILLS:
                    r = entry(it["name"])
                    if it["version"]:
                        r["versions"].append({"version": it["version"], "where": p["name"]})
                    if pid and pid not in r["projects"]:
                        r["projects"].append(pid)
                    r["location"] = r["location"] or p["location"]
        text = "\n".join([p["name"], p.get("tagline") or ""] + [b["text"] for b in p["bullets"]])
        for c in skills_taxonomy.extract_skills(text):
            r = reg.get(c) or entry(c)
            r["location"] = r["location"] or p["location"]
            if pid and pid not in r["projects"]:
                r["projects"].append(pid)
    for e in parsed["employment"]:
        eid = emp_ids.get(e["index"])
        text = "\n".join([e["title"]] + [b["text"] for b in e["bullets"]])
        for c in skills_taxonomy.extract_skills(text, ignore=[e["company"]]):
            r = reg.get(c) or entry(c)
            r["location"] = r["location"] or e["location"]
            if eid and eid not in r["employment"]:
                r["employment"].append(eid)
    return reg


def _skill_evidence(s: Session, r: dict) -> str:
    parts = []
    if r["resume_categories"]:
        parts.append(f"Resume claim: listed under {', '.join(repr(c) for c in r['resume_categories'])}")
    if r["versions"]:
        parts.append("versions stated: " + ", ".join(f"{v['version']} ({v['where']})" for v in r["versions"]))
    if r["parents"]:
        parts.append(f"listed as part of {', '.join(r['parents'])}")
    work = [f"{e.title} at {e.company}" for e in (s.get(Employment, i) for i in r["employment"]) if e]
    projs = [p.name for p in (s.get(Project, i) for i in r["projects"]) if p]
    if work:
        parts.append(f"mentioned in work bullets: {', '.join(work)}")
    if projs:
        parts.append(f"mentioned in projects: {', '.join(projs)}")
    if not r["listed"]:
        parts.insert(0, "Resume claim: not in the skills list")
    return "; ".join(parts)[:2000]


def _import_skills(run: _Run, parsed: dict, emp_ids: dict[int, int], proj_ids: dict[int, int]) -> None:
    for canon, r in _skill_registry(parsed, emp_ids, proj_ids).items():
        key = f"skill.{canon}"
        existing = run.s.scalar(select(Skill).where(Skill.normalized_name == canon))
        evidence = _skill_evidence(run.s, r)
        rel = {k: r[k] for k in ("canonical", "aliases", "versions", "resume_categories", "parents", "employment",
                                 "projects", "listed")}
        if existing is None:
            name = r["display"] if skills_taxonomy.canonical(r["display"]) == canon else canon
            cat = skills_taxonomy.category_of(canon) or (_key(r["resume_categories"][0])[:50] if r["resume_categories"]
                                                          else None)
            obj = run.save("skills", None, {"name": name, "category": cat, "evidence": evidence})
            eid = obj.id
            run.claim(key + ".evidence", category="skill", entity_type="skills", entity_id=eid, field="evidence",
                      value=evidence, original=None, location=r["location"], outcome="created")
        else:
            changes: dict = {}
            outcome = "unchanged"
            if existing.status != "confirmed" and (_empty(existing.evidence) or existing.evidence == run.prev_value(key + ".evidence")):
                if not _same(existing.evidence, evidence):
                    changes["evidence"] = evidence
                    outcome = "updated"
            if changes:
                run.save("skills", existing, changes)
            eid = existing.id
            run.claim(key + ".evidence", category="skill", entity_type="skills", entity_id=eid, field="evidence",
                      value=evidence, original=None, location=r["location"], outcome=outcome)
        run.claim(key, category="skill", entity_type="skills", entity_id=eid, field="name", value=r["display"],
                  original=r["original"], location=r["location"], outcome="created" if existing is None else "unchanged",
                  related=rel, notes=None if r["listed"] else "detected in the text, not listed under skills")


def _import_simple(run: _Run, parsed: dict) -> None:
    certs = list(run.s.scalars(select(Certification)))
    for c in parsed["certifications"]:
        key = f"certification.{_key(c['text'])[:60]}"
        match = next((x for x in certs if _same(x.name, c["text"])), None)
        if match is None:
            match = run.save("certifications", None, {"name": c["text"][:200]})
            certs.append(match)
            outcome = "created"
        else:
            outcome = "unchanged"
        run.claim(key, category="certification", entity_type="certifications", entity_id=match.id, field="name",
                  value=c["text"], original=c["original"], location=c["location"], outcome=outcome,
                  notes="issuer and date are not stated on the resume" if outcome == "created" else None)
    achs = list(run.s.scalars(select(Achievement)))
    for a in parsed["achievements"]:
        key = f"achievement.{_key(a['text'])[:60]}"
        match = next((x for x in achs if _same(x.title, a["text"]) or _same(x.description, a["text"])), None)
        if match is None:
            desc = a["text"] + (f"\nPlatforms: {', '.join(a['platforms'])}" if a.get("platforms") else "")
            match = run.save("achievements", None, {"title": a["text"][:200], "description": desc, "context": "other"})
            achs.append(match)
            outcome = "created"
        else:
            outcome = "unchanged"
        run.claim(key, category="achievement", entity_type="achievements", entity_id=match.id, field="title",
                  value=a["text"], original=a["original"], location=a["location"], outcome=outcome,
                  related={"metrics": a["metrics"], "platforms": a.get("platforms") or []})


def _import_answers(run: _Run, parsed: dict) -> None:
    """Unapproved answer-bank drafts built only from stated facts (your profile, the resume's own wording)."""
    from jobpilot.services import answers as answer_svc

    profile = profile_svc.get_profile(run.s)
    drafts = []
    if profile.notice_period_days is not None:
        drafts.append(("What is your notice period?", f"{profile.notice_period_days} days", "logistics",
                       "candidate_profile.notice_period_days"))
    if profile.willing_to_relocate is not None:
        ans = "Yes" if profile.willing_to_relocate else "No"
        if profile.willing_to_relocate and profile.acceptable_locations:
            ans += f". Preferred locations: {', '.join(profile.acceptable_locations)}"
        drafts.append(("Are you willing to relocate?", ans, "logistics",
                       "candidate_profile.willing_to_relocate, acceptable_locations"))
    if parsed["projects"]:
        p = parsed["projects"][0]
        head = p["name"] + (f" — {p['tagline']}" if p.get("tagline") else "")
        drafts.append(("Tell us about a project you are proud of.",
                       "\n".join([head + " (personal project)"] + [f"- {b['text']}" for b in p["bullets"]]),
                       "experience", f"resume · PROJECTS · {p['name']} (wording as on the resume)"))
    if parsed["employment"]:
        e = parsed["employment"][0]
        drafts.append(("Describe your experience with backend development.",
                       "\n".join([f"{e['title']}, {e['company']} ({_date_text(e['dates']) or 'dates not stated'})"] +
                                 [f"- {b['text']}" for b in e["bullets"]]),
                       "experience", f"resume · EXPERIENCE · {e['company']} (wording as on the resume)"))
    existing = {normalize(x.question): x for x in run.s.scalars(select(AnswerBankEntry))}
    for question, answer, category, grounded in drafts:
        key = f"answer.{_key(question)[:60]}"
        match = existing.get(normalize(question))
        if match is None:
            match = answer_svc.save_entry(run.s, question, answer, category=category, approved=False)
            outcome = "created"
        else:
            outcome = "unchanged"  # never rewrite an answer you already have
        run.claim(key, category="answer", entity_type="answers", entity_id=match.id, field="answer", value=answer,
                  original=None, location=grounded, outcome=outcome, state="unverified",
                  notes="draft: not approved, so never suggested for an application until you approve it")


def _verify(run: _Run) -> dict[str, Any]:
    """Read every linked record back and check what the report says was written is really there."""
    models = {k: m for k, (m, _) in profile_svc.FACT_MODELS.items()}
    models.update(profile=CandidateProfile, answers=AnswerBankEntry, resume_version=ResumeVersion)
    missing, checked = [], 0
    for c in run.prev.values():
        if c.import_id != run.imp.id or not c.entity_type or c.entity_id is None:
            continue
        checked += 1
        obj = run.s.get(models[c.entity_type], c.entity_id)
        if obj is None:
            missing.append(c.claim_key)
            continue
        if c.outcome in ("created", "updated") and c.field in ("description",) and \
                _key(c.value) not in _key(getattr(obj, c.field, "")):
            missing.append(c.claim_key)
    return {"checked": checked, "missing": missing}


def _counts(items: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {o: 0 for o in ("created", "updated", "unchanged", "conflict", "suggestion", "dismissed",
                                           "applied")}
    by_cat: dict[str, dict[str, int]] = {}
    for it in items:
        out[it["outcome"]] = out.get(it["outcome"], 0) + 1
        cat = by_cat.setdefault(it["category"], {})
        cat[it["outcome"]] = cat.get(it["outcome"], 0) + 1
    out["by_category"] = by_cat
    out["total"] = len(items)
    return out


def import_resume(session: Session, settings: Settings, filename: str, data: bytes, *, dry_run: bool = False,
                  backup: bool = True, use_pdftotext: bool = True) -> ProfileImport:
    """Import one resume file into the profile. Returns the persisted ``ProfileImport`` (with its report).

    ``dry_run``: do everything, report it, then roll the changes back (only the dry-run record is kept).
    Raises ``HubImportError`` for unreadable files (the failed attempt is recorded).
    """
    sha = hashlib.sha256(data or b"").hexdigest()
    label = _source_label(Path(filename or "resume").name, sha)
    backup_path = None
    if backup and not dry_run and settings.db_file and settings.db_file.exists():
        from jobpilot.services.backup import create_backup

        backup_path = str(create_backup(settings, label="pre-hub-import"))
    imp = ProfileImport(source_kind="resume", source_filename=Path(filename or "").name[:300], source_sha256=sha,
                        parser_version=resume_parser.PARSER_VERSION, status="completed", backup_path=backup_path)
    session.add(imp)
    session.flush()
    try:
        extracted = resume_parser.extract_document(filename, data, use_pdftotext=use_pdftotext)
    except resume_parser.ExtractionError as exc:
        imp.status, imp.error, imp.finished_at = "failed", str(exc), utcnow()
        audit.record(session, "hub.import_failed", "profile_import", imp.id, reason=str(exc)[:200])
        raise HubImportError(str(exc)) from exc
    imp.extraction_method = extracted["method"]
    skills_taxonomy.configure_from_settings(session)
    parsed = resume_parser.parse_resume(extracted["text"], extracted["links"])
    warnings = extracted["warnings"] + parsed["warnings"]
    if not parsed["header"]["name"] and not parsed["employment"] and not parsed["skills"]:
        imp.status, imp.error, imp.finished_at = "failed", "Nothing recognisable as a resume was found", utcnow()
        imp.report = {"warnings": warnings}
        raise HubImportError(imp.error)

    run = _Run(session, imp, f"resume:{sha}", label)
    sp = session.begin_nested()
    try:
        if dry_run and not session.scalar(select(ResumeVersion.id).where(ResumeVersion.sha256 == sha)):
            version = None  # a dry run never stores the file
            run.claim("resume_version", category="resume", entity_type=None, entity_id=None, field="file",
                      value=Path(filename).name, original=None, location="file", outcome="created",
                      notes="would be registered as a new draft version of the master resume")
        else:
            version = _register_version(run, settings, filename, data, sha)
        _import_profile(run, parsed)
        _import_links(run, parsed)
        emp_ids = _import_employment(run, parsed)
        _import_education(run, parsed)
        proj_ids = _import_projects(run, parsed)
        _import_skills(run, parsed, emp_ids, proj_ids)
        _import_simple(run, parsed)
        _import_answers(run, parsed)
        session.flush()
        verification = _verify(run)
        if verification["missing"]:
            raise HubImportError(f"Verification failed for {len(verification['missing'])} record(s): "
                                 f"{', '.join(verification['missing'][:5])}")
        if dry_run:
            sp.rollback()
        else:
            sp.commit()
    except Exception as exc:
        if sp.is_active:
            sp.rollback()
        imp.status, imp.error, imp.finished_at = "failed", f"{type(exc).__name__}: {exc}"[:2000], utcnow()
        audit.record(session, "hub.import_failed", "profile_import", imp.id, reason=type(exc).__name__)
        raise HubImportError(str(exc)) from exc
    imp.status = "dry_run" if dry_run else "completed"
    imp.resume_version_id = version.id if version is not None and not dry_run else None
    imp.counts = _counts(run.items)
    imp.report = {"items": run.items, "warnings": warnings, "verification": verification,
                  "sections_found": parsed["sections_found"], "source_label": label}
    imp.finished_at = utcnow()
    session.flush()
    audit.record(session, "hub.import_dry_run" if dry_run else "hub.imported", "profile_import", imp.id,
                 **{k: v for k, v in imp.counts.items() if k != "by_category"})
    return imp


def import_registered_version(session: Session, settings: Settings, version: ResumeVersion, **kw) -> ProfileImport:
    """Re-run the import from a resume version already in the library (its stored, unmodified file)."""
    try:
        path = storage.resolve(settings, version.file_path)
    except storage.StorageError as exc:
        raise HubImportError(f"The stored file for {version.label} is missing") from exc
    return import_resume(session, settings, version.original_filename or path.name, path.read_bytes(), **kw)


# ---- Repository documentation for a project -------------------------------------------------------------

def link_project_docs(session: Session, project: Project, path: str | Path) -> list[ProfileClaim]:
    """Reference a local project's own documentation (README, docs/*.md, git remote) without copying it.

    Only the README's title/first paragraph, the names of files under ``docs/`` and the ``origin`` URL from
    ``.git/config`` are read. Source code and other files are never read.
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    ref = f"repo:{root}"
    run_claims = []
    now = utcnow()

    def put(key: str, field: str, value: str, location: str, notes: str | None = None) -> None:
        c = session.scalar(select(ProfileClaim).where(ProfileClaim.source_ref == ref, ProfileClaim.claim_key == key)) \
            or ProfileClaim(source_ref=ref, claim_key=key)
        session.add(c)
        c.category, c.entity_type, c.entity_id, c.field = "project", "projects", project.id, field
        c.value, c.original_text, c.source_kind, c.source_label = value, None, "repository", str(root)[:300]
        c.source_location, c.state, c.outcome, c.notes, c.extracted_at = location, "unverified", "unchanged", notes, now
        run_claims.append(c)

    readme = next((root / n for n in ("README.md", "README.rst", "README.txt", "README") if (root / n).is_file()), None)
    if readme:
        text = readme.read_text(errors="replace")[:20000]
        title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), None)
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not p.lstrip().startswith(("#", "```", "!["))]
        put("readme", "documentation", str(readme), "README", notes=f"title: {title}" if title else None)
        if paras:
            put("readme.summary", "repository_description", resume_parser.clean_text(paras[0])[:1000], f"{readme.name} · first paragraph",
                notes="the repository's own description, kept separate from the resume's wording")
    docs = sorted(p.name for p in (root / "docs").glob("*.md")) if (root / "docs").is_dir() else []
    if docs:
        put("docs", "documentation", ", ".join(docs), "docs/", notes=f"{len(docs)} document(s) in {root / 'docs'}")
    git_config = root / ".git" / "config"
    if git_config.is_file():
        m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', git_config.read_text(errors="replace"))
        if m:
            remote = re.sub(r"\.git$", "", m.group(1))
            same = bool(project.url) and _same_url(project.url, remote)
            put("git.origin", "repository_url", remote, ".git/config · origin",
                notes="matches the project URL on the resume" if same else
                ("differs from the project URL on file" if project.url else "no project URL on file"))
    session.flush()
    audit.record(session, "hub.project_docs_linked", "projects", project.id, files=len(run_claims))
    return run_claims


# ---- Resolving conflicts and suggestions -----------------------------------------------------------------

def open_conflicts(session: Session) -> list[str]:
    """Short labels of values where your resume and your profile disagree (used by preparation and the queue)."""
    return [f"{c.category} {c.field}: resume says “{(c.value or '')[:60]}”, profile has “{(c.existing_value or '')[:60]}”"
            for c in session.scalars(select(ProfileClaim).where(ProfileClaim.outcome == "conflict")
                                     .order_by(ProfileClaim.id))]


def apply_claim(session: Session, claim: ProfileClaim) -> None:
    """Write a conflicting or suggested value into its fact (your decision), through the profile services."""
    if claim.outcome not in OPEN_OUTCOMES:
        raise ValueError("Only conflicts and suggestions can be applied")
    if claim.entity_type == "profile":
        profile = profile_svc.get_profile(session)
        data = {f: getattr(profile, f) for f in profile_svc.ProfileIn.model_fields}
        data[claim.field] = claim.value
        profile_svc.update_profile(session, profile_svc.ProfileIn.model_validate(data))
    elif claim.entity_type in profile_svc.FACT_MODELS:
        model, schema = profile_svc.FACT_MODELS[claim.entity_type]
        obj = session.get(model, claim.entity_id)
        if obj is None:
            raise LookupError("The record this value belongs to no longer exists")
        values = {f: getattr(obj, f) for f in schema.model_fields if hasattr(obj, f)}
        if claim.field in ("description", "notes"):
            values[claim.field] = "\n".join(x for x in (values.get(claim.field), claim.value) if x)
        elif claim.field == "dates":
            raise ValueError("Edit the dates on the record itself: the resume states them only to the month or year")
        else:
            values[claim.field] = claim.value
        profile_svc.save_fact(session, claim.entity_type, schema.model_validate(values), obj.id)
    else:
        raise ValueError("This value cannot be applied automatically")
    claim.outcome, claim.resolved_at = "applied", utcnow()
    audit.record(session, "hub.claim_applied", "profile_claim", claim.id, field=claim.field)


def dismiss_claim(session: Session, claim: ProfileClaim) -> None:
    """Keep the profile's value; the resume's value is marked rejected and not raised again."""
    if claim.outcome not in OPEN_OUTCOMES:
        raise ValueError("Only conflicts and suggestions can be dismissed")
    claim.outcome, claim.state, claim.resolved_at = "dismissed", "rejected", utcnow()
    audit.record(session, "hub.claim_dismissed", "profile_claim", claim.id, field=claim.field)


# ---- Reading: one view over the profile ------------------------------------------------------------------

def _entity(session: Session, claim: ProfileClaim):
    if claim.entity_type == "profile":
        return profile_svc.get_profile(session)
    if claim.entity_type in profile_svc.FACT_MODELS:
        return session.get(profile_svc.FACT_MODELS[claim.entity_type][0], claim.entity_id)
    return None


def fact_state(obj: Any) -> str:
    """Hub state of a profile fact from its own status."""
    if isinstance(obj, CandidateProfile):
        return "verified" if obj.confirmed_at else "unverified"
    status = getattr(obj, "status", None)
    return {"confirmed": "verified", "learning": "learning", "outdated": "outdated"}.get(status, "unverified")


def claim_state(claim: ProfileClaim, obj: Any = None) -> str:
    """The claim's state, taking the fact's status into account when the fact holds the claimed value."""
    if claim.state == "rejected" or claim.outcome == "dismissed":
        return "rejected"
    if claim.outcome in OPEN_OUTCOMES or obj is None:
        return claim.state
    fs = fact_state(obj)
    return fs if fs != "unverified" else claim.state


def claims_by_entity(session: Session) -> dict[tuple[str, int], list[ProfileClaim]]:
    out: dict[tuple[str, int], list[ProfileClaim]] = {}
    for c in session.scalars(select(ProfileClaim).order_by(ProfileClaim.id)):
        if c.entity_type and c.entity_id is not None:
            out.setdefault((c.entity_type, c.entity_id), []).append(c)
    return out


def search_claims(session: Session, q: str | None = None, state: str | None = None, category: str | None = None,
                  limit: int = 500) -> list[dict[str, Any]]:
    stmt = select(ProfileClaim).order_by(ProfileClaim.category, ProfileClaim.id)
    if category:
        stmt = stmt.where(ProfileClaim.category == category)
    if q:
        like = f"%{q.strip()[:100]}%"
        stmt = stmt.where(or_(ProfileClaim.value.ilike(like), ProfileClaim.original_text.ilike(like),
                              ProfileClaim.source_location.ilike(like), ProfileClaim.field.ilike(like)))
    out = []
    for c in session.scalars(stmt):
        st = claim_state(c, _entity(session, c))
        if state and st != state and not (state == "open" and c.outcome in OPEN_OUTCOMES):
            continue
        out.append({"claim": c, "state": st})
        if len(out) >= limit:
            break
    return out


def skill_rows(session: Session) -> list[dict[str, Any]]:
    """Skill registry: every skill with its status, how it is evidenced, stated versions and sources."""
    by_skill = {c.entity_id: c for c in session.scalars(select(ProfileClaim).where(
        ProfileClaim.entity_type == "skills", ProfileClaim.field == "name").order_by(ProfileClaim.id))}
    evidence = profile_svc.skill_profile(session)  # confirmed facts only
    emp = {e.id: e for e in session.scalars(select(Employment))}
    proj = {p.id: p for p in session.scalars(select(Project))}
    rows = []
    for sk in profile_svc.list_facts(session, "skills"):
        c = by_skill.get(sk.id)
        rel = (c.related or {}) if c else {}
        claimed_work = [f"{emp[i].title} at {emp[i].company}" for i in rel.get("employment", []) if i in emp]
        claimed_proj = [proj[i].name for i in rel.get("projects", []) if i in proj]
        ev = evidence.get(sk.normalized_name, {})
        usage = []
        if ev.get("work"):
            usage.append("used professionally (verified role)")
        elif claimed_work:
            usage.append("used professionally (resume claim)")
        if ev.get("projects") or ev.get("non_work"):
            usage.append("used in a personal project (verified project)")
        elif claimed_proj:
            usage.append("used in a personal project (resume claim)")
        if sk.status == "learning":
            usage.append("currently learning")
        if not usage:
            usage.append("listed only" if rel.get("listed") else "mentioned, no evidence")
        rows.append({"skill": sk, "state": fact_state(sk) if sk.status != "unverified" else
                     ("resume_claim" if c else "unverified"), "usage": usage, "versions": rel.get("versions", []),
                     "resume_categories": rel.get("resume_categories", []), "aliases": rel.get("aliases", []),
                     "work": ev.get("work") or claimed_work, "projects": (ev.get("projects") or []) + (ev.get("non_work") or [])
                     or claimed_proj, "claim": c})
    return rows


def validation_issues(session: Session) -> list[dict[str, str]]:
    """Missing or contradictory information to fix before it causes an inaccurate application."""
    p = profile_svc.get_profile(session)
    issues: list[dict[str, str]] = []

    def add(sev: str, area: str, msg: str, link: str = "/profile") -> None:
        issues.append({"severity": sev, "area": area, "message": msg, "link": link})

    n_open = session.scalar(select(func.count(ProfileClaim.id)).where(ProfileClaim.outcome == "conflict")) or 0
    if n_open:
        add("warn", "conflicts", f"{n_open} value(s) on your resume differ from your profile: resolve them below", "/hub#attention")
    if p.id is None or not p.confirmed_at:
        add("warn", "profile", "Core details are not confirmed, so applications cannot use them yet")
    if not p.preferred_titles:
        add("info", "preferences", "Target job titles are not recorded (Profile → Preferred job titles)")
    if not p.job_search_priorities:
        add("info", "preferences", "Job-search priorities are not recorded")
    if p.salary_expectation_min is None and p.salary_expectation_max is None:
        add("info", "preferences", "Salary expectation is not recorded")
    roles = list(session.scalars(select(Employment).where(Employment.status != "outdated")))
    for e in roles:
        if not e.role_type:
            add("warn", "employment", f"Kind of work is not recorded for {e.title} at {e.company}: its years cannot "
                                      "count as development experience", f"/profile/employment/{e.id}/edit")
        if e.status != "confirmed":
            add("info", "employment", f"{e.title} at {e.company} is not confirmed", "/profile#employment")
    stated = p.total_experience_years
    if stated is not None:
        confirmed = [e for e in roles if e.status == "confirmed" and e.start_date and e.employment_type != "internship"]
        if confirmed:
            computed = sum(max(0, ((e.end_date or date.today()) - e.start_date).days) for e in confirmed) / 365.25
            if abs(computed - stated) > 0.25:
                add("warn", "experience", f"Stated total experience is {stated:g} years, but your confirmed roles add up "
                                          f"to {computed:.1f} years")
    n_unv = session.scalar(select(func.count(Skill.id)).where(Skill.status == "unverified")) or 0
    if n_unv:
        add("info", "skills", f"{n_unv} skill(s) are unconfirmed and are not used for matching", "/hub#skills")
    for kind, model, label in (("projects", Project, "project"), ("certifications", Certification, "certification"),
                               ("achievements", Achievement, "achievement"), ("education", Education, "education entry")):
        n = session.scalar(select(func.count(model.id)).where(model.status == "unverified")) or 0
        if n:
            add("info", kind, f"{n} {label}(s) imported but not confirmed", f"/hub#{kind}")
    for ed in session.scalars(select(Education).where(Education.status == "confirmed", Education.grade.is_(None))):
        add("info", "education", f"No grade recorded for {ed.degree or ''} at {ed.institution}", f"/profile/education/{ed.id}/edit")
    n_links = session.scalar(select(func.count(ProfileLink.id)).where(ProfileLink.status == "unverified")) or 0
    if n_links:
        add("info", "links", f"{n_links} profile link(s) imported but not confirmed", "/hub#profile")
    return issues


def overview(session: Session) -> dict[str, Any]:
    """Everything the hub shows, read from the profile tables plus provenance."""
    claims = claims_by_entity(session)
    profile = profile_svc.get_profile(session)

    def facts(kind: str) -> list[dict[str, Any]]:
        out = []
        for obj in profile_svc.list_facts(session, kind):
            cs = claims.get((kind, obj.id), [])
            out.append({"obj": obj, "state": fact_state(obj) if obj.status != "unverified" or not cs else
                        "resume_claim", "claims": [{"claim": c, "state": claim_state(c, obj)} for c in cs],
                        "sources": sorted({c.source_label for c in cs if c.source_label})})
        return out

    profile_claims = {}
    if profile.id is not None:
        for c in claims.get(("profile", profile.id), []):
            profile_claims.setdefault(c.field, []).append({"claim": c, "state": claim_state(c, profile)})
    attention = [{"claim": c, "state": claim_state(c), "entity": _entity(session, c)}
                 for c in session.scalars(select(ProfileClaim).where(ProfileClaim.outcome.in_(OPEN_OUTCOMES))
                                          .order_by(ProfileClaim.outcome, ProfileClaim.category, ProfileClaim.id))]
    projects = facts("projects")
    for row in projects:
        obj = row["obj"]
        bullets = [ln.strip() for ln in (obj.description or "").splitlines() if ln.strip()]
        row["bullets"] = bullets
        row["metrics"] = sorted({m for c in row["claims"] for m in ((c["claim"].related or {}).get("metrics") or [])})
        row["docs"] = [c for c in row["claims"] if c["claim"].source_kind == "repository"]
        row["talking_points"] = [f"{obj.name}: walk through the problem, your design and what you would change",
                                 *[f"Be ready to explain how you measured: “{b}”" for b in bullets
                                   if resume_parser.metrics_in(b)][:4]]
    versions = list(session.scalars(select(ResumeVersion).order_by(ResumeVersion.id.desc())))
    imports = list(session.scalars(select(ProfileImport).order_by(ProfileImport.id.desc()).limit(20)))
    learning = list(session.scalars(select(LearningItem).order_by(LearningItem.priority.desc().nulls_last(),
                                                                  LearningItem.id)))
    answers = list(session.scalars(select(AnswerBankEntry).order_by(AnswerBankEntry.id)))
    history = list(session.scalars(select(AuditLog).where(or_(AuditLog.action.like("fact.%"),
                                                              AuditLog.action.like("profile.%"),
                                                              AuditLog.action.like("hub.%")))
                                   .order_by(AuditLog.id.desc()).limit(25)))
    all_claims = list(session.scalars(select(ProfileClaim)))
    states: dict[str, int] = {}
    for c in all_claims:
        st = claim_state(c, _entity(session, c))
        states[st] = states.get(st, 0) + 1
    sources: dict[str, int] = {}
    for c in all_claims:
        sources[c.source_label or c.source_kind] = sources.get(c.source_label or c.source_kind, 0) + 1
    return {
        "profile": profile, "profile_claims": profile_claims,
        "links": facts("links"), "employment": facts("employment"), "education": facts("education"),
        "projects": projects, "certifications": facts("certifications"), "achievements": facts("achievements"),
        "skills": skill_rows(session), "attention": attention, "issues": validation_issues(session),
        "versions": versions, "imports": imports, "learning": learning,
        "learning_skills": sorted(profile_svc.learning_skills(session)), "answers": answers, "history": history,
        "states": states, "sources": sources, "claim_count": len(all_claims),
        "experience": profile_svc.experience_breakdown(session), "years": profile_svc.experience_years(session),
    }


def snapshot(session: Session) -> dict[str, Any]:
    """JSON-safe summary of the hub (for the API)."""
    ov = overview(session)
    p = ov["profile"]

    def fact(row: dict, fields: tuple[str, ...]) -> dict:
        obj = row["obj"]
        d = {f: (getattr(obj, f).isoformat() if isinstance(getattr(obj, f), date) else getattr(obj, f)) for f in fields}
        d.update(id=obj.id, status=obj.status, state=row["state"], sources=row["sources"],
                 claims=[{"field": c["claim"].field, "value": c["claim"].value, "state": c["state"],
                          "outcome": c["claim"].outcome, "location": c["claim"].source_location} for c in row["claims"]])
        return d

    return {
        "profile": {f: getattr(p, f) for f in profile_svc.ProfileIn.model_fields} | {
            "confirmed": p.confirmed_at is not None,
            "provenance": {k: [{"value": c["claim"].value, "state": c["state"], "outcome": c["claim"].outcome,
                                "source": c["claim"].source_label, "location": c["claim"].source_location}
                               for c in v] for k, v in ov["profile_claims"].items()}},
        "links": [fact(r, ("label", "url")) for r in ov["links"]],
        "employment": [fact(r, ("company", "title", "employment_type", "role_type", "start_date", "end_date",
                                "description")) for r in ov["employment"]],
        "education": [fact(r, ("institution", "degree", "field_of_study", "start_date", "end_date", "grade", "notes"))
                      for r in ov["education"]],
        "projects": [fact(r, ("name", "kind", "url", "technologies", "start_date", "end_date", "description"))
                     for r in ov["projects"]],
        "certifications": [fact(r, ("name", "issuer", "issued_on")) for r in ov["certifications"]],
        "achievements": [fact(r, ("title", "description", "context")) for r in ov["achievements"]],
        "skills": [{"name": r["skill"].name, "canonical": r["skill"].normalized_name, "status": r["skill"].status,
                    "state": r["state"], "usage": r["usage"], "versions": r["versions"],
                    "resume_categories": r["resume_categories"], "work": r["work"], "projects": r["projects"]}
                   for r in ov["skills"]],
        "unconfirmed_skills": sorted(r["skill"].name for r in ov["skills"] if r["skill"].status == "unverified"),
        "attention": [{"id": a["claim"].id, "outcome": a["claim"].outcome, "category": a["claim"].category,
                       "field": a["claim"].field, "resume_value": a["claim"].value,
                       "profile_value": a["claim"].existing_value, "location": a["claim"].source_location}
                      for a in ov["attention"]],
        "issues": ov["issues"], "states": ov["states"],
        "resume_versions": [{"id": v.id, "label": v.label, "status": v.status, "sha256": v.sha256,
                             "original_filename": v.original_filename} for v in ov["versions"]],
        "imports": [{"id": i.id, "status": i.status, "file": i.source_filename, "sha256": i.source_sha256,
                     "method": i.extraction_method, "counts": i.counts, "at": i.created_at.isoformat()}
                    for i in ov["imports"]],
    }
