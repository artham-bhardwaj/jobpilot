"""Milestone 12: personal information hub — resume extraction, provenance, idempotent import, integration.

Synthetic data only: "Test Candidate" and fictional employers, schools and projects.
"""
from __future__ import annotations

import shutil
from datetime import date

import pytest
from sqlalchemy import func, select, text

from jobpilot.models import (Achievement, AnswerBankEntry, Application, Certification, Education, Employment,
                             ProfileClaim, ProfileImport, ProfileLink, Project, ResumeVersion, Skill, utcnow)
from jobpilot.services import hub, profile as ps, resume_parser as rp
from jobpilot.services.skills_taxonomy import canonical, extract_skills
from tests.helpers import make_pdf_bytes, seed_profile

RESUME_LINES = [
    "Test Candidate",
    "Backend Engineer",
    "test.candidate@example.com | +91 90000 00000 | LinkedIn | GitHub",
    "EXPERIENCE",
    "* Software Engineer, Example Systems (EXS)    Jan 2024 - Present",
    "- Built Java and Spring Boot services exposing 20+ REST APIs for the billing module, with inte-",
    "  gration tests for every endpoint.",
    "- Reduced build time by 30% with CI/CD caching.",
    "TECHNICAL SKILLS",
    "* Languages: Java (17), Python (3), SQL",
    "* Backend & Data: Spring Boot (3.x), JPA/Hibernate, Kafka, PostgreSQL, MySQL, Prometheus",
    "PROJECTS",
    "* Widget Tracker - Inventory Demo    Mar 2025 - May 2025",
    "Python, FastAPI, Redis    GitHub",
    "- Built a FastAPI service with Redis caching serving 100+ requests per second.",
    "ACHIEVEMENTS",
    "* Solved 500+ algorithm problems across LeetCode and Codeforces.",
    "CERTIFICATIONS",
    "* Example Cloud Fundamentals",
    "EDUCATION",
    "* B.Tech, Computer Science - Example Institute of Technology, Pune    2019 - 2023",
    "CGPA: 8.10/10",
    "* Class XII (Science) - CBSE    2019",
    "Score: 90%",
]
LINKS = [("https://www.linkedin.com/in/test-candidate", 300, 752), ("https://github.com/test-candidate", 360, 752),
         ("https://github.com/test-candidate/widget-tracker", 400, 598)]


def resume_pdf(lines=None, links=None) -> bytes:
    return make_pdf_bytes(lines or RESUME_LINES, links=LINKS if links is None else links)


def _import(db, settings, data=None, name="candidate.pdf", **kw):
    kw.setdefault("backup", False)
    imp = hub.import_resume(db, settings, name, data or resume_pdf(), **kw)
    db.commit()
    return imp


def _counts(db):
    return {m.__name__: db.scalar(select(func.count(m.id))) for m in (
        Employment, Education, Skill, Project, Certification, Achievement, ProfileLink, ResumeVersion, AnswerBankEntry,
        ProfileClaim)}


# ---- Extraction --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("use_pdftotext", [True, False])
def test_pdf_text_and_link_extraction(use_pdftotext):
    if use_pdftotext and not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    out = rp.extract_document("cv.pdf", resume_pdf(), use_pdftotext=use_pdftotext)
    assert out["method"] == ("pdftotext" if use_pdftotext else "pypdf")
    assert "Test Candidate" in out["text"] and "Example Systems" in out["text"]
    assert [li["uri"] for li in out["links"]] == [u for u, _, _ in LINKS], "reading order: top to bottom, left to right"
    assert out["page_count"] == 1
    if not use_pdftotext:
        assert out["warnings"], "the pypdf fallback is reported, since it can drop spaces around bold text"


@pytest.mark.parametrize("name,data,msg", [
    ("cv.pdf", b"", "empty"),
    ("cv.pdf", b"hello, not a pdf", "does not look like a PDF"),
    ("cv.pdf", b"%PDF-1.4 garbage", "Could not read PDF"),
    ("cv.exe", b"MZ", "Unsupported"),
    ("cv.txt", b"\xff\xfe\x00bad", "UTF-8"),
])
def test_malformed_files_are_rejected_clearly(name, data, msg):
    with pytest.raises(rp.ExtractionError, match=msg):
        rp.extract_document(name, data)


# ---- Parsing -------------------------------------------------------------------------------------------------

def _parsed():
    out = rp.extract_document("cv.pdf", resume_pdf())
    return rp.parse_resume(out["text"], out["links"])


def test_header_contact_and_links():
    p = _parsed()
    h = p["header"]
    assert (h["name"], h["headline"], h["email"], h["phone"]) == (
        "Test Candidate", "Backend Engineer", "test.candidate@example.com", "+91 90000 00000")
    assert [(li["label"], li["url"]) for li in p["links"]] == [
        ("LinkedIn", "https://www.linkedin.com/in/test-candidate"), ("GitHub", "https://github.com/test-candidate")]
    assert p["sections_found"] == ["experience", "skills", "projects", "achievements", "certifications", "education"]
    assert p["warnings"] == []


def test_employment_extraction_keeps_every_bullet_and_its_location():
    (e,) = _parsed()["employment"]
    assert (e["title"], e["company"], e["company_alias"]) == ("Software Engineer", "Example Systems", "EXS")
    assert e["dates"]["start"] == {"text": "Jan 2024", "year": 2024, "month": 1, "precision": "month"}
    assert e["dates"]["end"]["current"] is True
    assert [b["text"] for b in e["bullets"]] == [
        "Built Java and Spring Boot services exposing 20+ REST APIs for the billing module, with integration tests "
        "for every endpoint.",
        "Reduced build time by 30% with CI/CD caching."]
    assert "inte-\ngration" in e["bullets"][0]["original"], "the original line breaks are kept for reference"
    assert e["bullets"][1]["location"] == "page 1 · EXPERIENCE · Example Systems · bullet 2"
    assert e["bullets"][1]["metrics"] == ["30%"]


def test_project_extraction():
    (p,) = _parsed()["projects"]
    assert (p["name"], p["tagline"]) == ("Widget Tracker", "Inventory Demo")
    assert p["technologies"] == ["Python", "FastAPI", "Redis"] and p["link_label"] == "GitHub"
    assert p["url"] == "https://github.com/test-candidate/widget-tracker"
    assert (p["dates"]["start"]["month"], p["dates"]["end"]["month"]) == (3, 5)
    assert p["bullets"][0]["metrics"] == ["100+"]


def test_education_extraction_keeps_stated_precision():
    btech, xii = _parsed()["education"]
    assert (btech["degree"], btech["field_of_study"], btech["institution"], btech["place"], btech["grade"]) == (
        "B.Tech", "Computer Science", "Example Institute of Technology", "Pune", "CGPA: 8.10/10")
    assert btech["dates"]["start"]["precision"] == "year" and rp.to_date(btech["dates"]["start"]) is None, \
        "a year is never turned into an invented exact date"
    assert (xii["degree"], xii["field_of_study"], xii["institution"], xii["grade"]) == ("Class XII", "Science", "CBSE",
                                                                                         "Score: 90%")


def test_skills_achievements_certifications():
    p = _parsed()
    items = {(i["name"], i["version"]) for g in p["skills"] for i in g["items"]}
    assert {("Java", "17"), ("Python", "3"), ("Spring Boot", "3.x"), ("JPA", None), ("Hibernate", None)} <= items
    assert [g["category"] for g in p["skills"]] == ["Languages", "Backend & Data"]
    assert p["certifications"][0]["text"] == "Example Cloud Fundamentals"
    a = p["achievements"][0]
    assert a["platforms"] == ["LeetCode", "Codeforces"] and a["metrics"] == ["500+"]


def test_missing_and_malformed_sections_never_crash():
    p = rp.parse_resume("Someone\nSKILLS\nnot a bullet list at all\nWEIRD SECTION\n* stuff", [])
    assert p["header"]["name"] == "Someone"
    assert "No experience section found" in p["warnings"] and "No education section found" in p["warnings"]
    empty = rp.parse_resume("", [])
    assert "No name found at the top of the resume" in empty["warnings"]
    # More body links than project link labels: nothing is assigned rather than guessing.
    q = rp.parse_resume("A B\na@example.com\nPROJECTS\n* P1 - x\nPython", [{"uri": "https://x.dev", "page": 1, "x": 0, "y": 0}])
    assert q["projects"][0]["url"] is None


def test_join_lines_and_dates():
    assert rp.join_lines(["integration across enter-", "prise systems"]) == "integration across enterprise systems"
    assert rp.join_lines(["Spring", "Boot"]) == "Spring Boot"
    assert rp.parse_partial_date("Sep 2026") == {"text": "Sep 2026", "year": 2026, "month": 9, "precision": "month"}
    assert rp.to_date(rp.parse_partial_date("Sep 2026")) == date(2026, 9, 1)
    assert rp.parse_partial_date("Present") == {"text": "Present", "current": True}
    assert rp.parse_partial_date("someday") is None


# ---- Skill normalisation ---------------------------------------------------------------------------------------

def test_skill_normalisation_is_careful():
    assert canonical("Java") == canonical("java 21") == canonical("Core Java") == "java"
    assert canonical("Quantum Basket Weaving") == "quantum basket weaving", "unknown names are not forced onto a skill"
    assert canonical("JPA") == canonical("Hibernate") == "hibernate"
    assert canonical("PostgreSQL") != canonical("MySQL")
    assert canonical("REST APIs") == "rest api" and canonical("Spring MVC") == "spring"
    assert canonical("Prometheus") == "prometheus" and canonical("Grafana") == "grafana", \
        "monitoring tools are distinct skills, not a generic 'observability'"
    assert canonical("Spring Actuator") == "spring boot actuator" and canonical("LLM Integration") == "llm"
    assert extract_skills("Built an MCP tool layer; RAG (TF-IDF, Embeddings)") == ["mcp", "rag", "tf-idf", "embeddings"]
    assert [i["name"] for i in rp.parse_skill_item("CI/CD")] == ["CI/CD"], "known slash names are not split"
    assert [(i["name"], i["parent"]) for i in rp.parse_skill_item("RAG (TF-IDF, Embeddings)")] == [
        ("RAG", None), ("TF-IDF", "RAG"), ("Embeddings", "RAG")]
    assert rp.parse_skill_item("Spring Boot 3.5")[0]["version"] == "3.5"


# ---- End to end: import, persist, reload, re-import -----------------------------------------------------------

def test_end_to_end_import_is_persisted_traceable_and_idempotent(app, settings):
    factory = app.state.sessionmaker
    with factory() as db:
        imp = hub.import_resume(db, settings, "candidate.pdf", resume_pdf(), backup=True)
        db.commit()
        first_counts = dict(imp.counts)
        assert imp.status == "completed" and imp.backup_path, "a backup is taken before changing the profile"
        assert first_counts["conflict"] == 0 and first_counts["created"] > 20
        assert imp.report["verification"]["missing"] == []

    with factory() as db:  # reload from the database in a fresh session
        p = ps.get_profile(db)
        assert (p.full_name, p.first_name, p.last_name, p.headline, p.email) == (
            "Test Candidate", "Test", "Candidate", "Backend Engineer", "test.candidate@example.com")
        assert p.confirmed_at is None, "an import never confirms your details"
        (emp,) = db.scalars(select(Employment)).all()
        assert (emp.company, emp.title, emp.status, emp.start_date, emp.end_date) == (
            "Example Systems", "Software Engineer", "unverified", date(2024, 1, 1), None)
        assert emp.role_type is None and emp.employment_type is None, "kind and type of work are not guessed"
        assert "Reduced build time by 30% with CI/CD caching." in emp.description
        assert ps.experience_years(db)[0] is None, "unverified roles never count as experience"
        edu = {e.degree: e for e in db.scalars(select(Education))}
        assert edu["B.Tech"].grade == "CGPA: 8.10/10" and edu["B.Tech"].start_date is None
        assert "2019 - 2023 (year only)" in edu["B.Tech"].notes and "Location: Pune" in edu["B.Tech"].notes
        assert "school is not named" in edu["Class XII"].notes
        (proj,) = db.scalars(select(Project)).all()
        assert (proj.name, proj.kind, proj.status, proj.url) == ("Widget Tracker", "personal", "unverified",
                                                                 "https://github.com/test-candidate/widget-tracker")
        assert proj.technologies == ["Python", "FastAPI", "Redis"]
        assert proj.description.splitlines()[0] == "Inventory Demo"
        skills = {s.normalized_name: s for s in db.scalars(select(Skill))}
        assert {"java", "python", "sql", "spring boot", "hibernate", "kafka", "postgresql", "mysql", "prometheus",
                "fastapi", "redis", "rest api", "ci/cd"} <= set(skills)
        assert all(s.status == "unverified" for s in skills.values()), "a resume never verifies a skill"
        assert "versions stated: 3.x (TECHNICAL SKILLS)" in skills["spring boot"].evidence
        assert "mentioned in work bullets: Software Engineer at Example Systems" in skills["java"].evidence
        assert "mentioned in projects: Widget Tracker" in skills["redis"].evidence
        assert db.scalar(select(Certification)).name == "Example Cloud Fundamentals"
        ach = db.scalar(select(Achievement))
        assert ach.title.startswith("Solved 500+") and "Platforms: LeetCode, Codeforces" in ach.description
        links = {li.label: li for li in db.scalars(select(ProfileLink))}
        assert links["GitHub"].status == "unverified"
        (ver,) = db.scalars(select(ResumeVersion)).all()
        assert ver.status == "draft" and ver.resume.is_master, "registered, but approving it is your decision"
        assert (settings.files_dir / ver.file_path).read_bytes() == resume_pdf(), "the original file is stored unchanged"
        answers = {a.question: a for a in db.scalars(select(AnswerBankEntry))}
        assert all(not a.approved for a in answers.values())
        assert "Widget Tracker" in answers["Tell us about a project you are proud of."].answer
        # Provenance: every claim says where it came from.
        bullet = db.scalar(select(ProfileClaim).where(ProfileClaim.claim_key == "employment.examplesystems.bullet[1]"))
        assert bullet.source_ref == f"resume:{ver.sha256}" and bullet.state == "resume_claim"
        assert bullet.source_location == "page 1 · EXPERIENCE · Example Systems · bullet 2"
        assert bullet.original_text == "Reduced build time by 30% with CI/CD caching."
        assert bullet.entity_type == "employment" and bullet.entity_id == emp.id
        before = _counts(db)

    with factory() as db:  # the same file again: nothing new
        imp2 = _import(db, settings)
        assert imp2.counts["created"] == imp2.counts["updated"] == imp2.counts["conflict"] == 0
        assert imp2.counts["total"] == first_counts["total"]
        assert _counts(db) == before
        assert db.scalar(select(func.count(ProfileImport.id))) == 2


def test_dry_run_reports_without_saving(db, settings):
    imp = _import(db, settings, dry_run=True)
    assert imp.status == "dry_run" and imp.counts["created"] > 20
    assert db.scalar(select(func.count(Employment.id))) == 0 and db.scalar(select(func.count(ResumeVersion.id))) == 0
    assert db.scalar(select(func.count(ProfileClaim.id))) == 0
    assert not list((settings.files_dir / "resumes").glob("*")) if (settings.files_dir / "resumes").exists() else True


def test_failed_import_changes_nothing(db, settings, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated failure half-way")

    monkeypatch.setattr(hub, "_import_skills", boom)
    with pytest.raises(hub.HubImportError, match="simulated"):
        hub.import_resume(db, settings, "cv.pdf", resume_pdf(), backup=False)
    db.commit()
    assert db.scalar(select(func.count(Employment.id))) == 0, "the savepoint rolled back the partial import"
    assert db.scalar(select(func.count(ProfileClaim.id))) == 0
    imp = db.scalar(select(ProfileImport))
    assert imp.status == "failed" and "simulated" in imp.error, "the failed attempt itself is recorded"


def test_unreadable_file_is_recorded_as_failed(db, settings):
    with pytest.raises(hub.HubImportError):
        hub.import_resume(db, settings, "cv.pdf", b"%PDF-1.4 broken", backup=False)
    db.commit()
    assert db.scalar(select(ProfileImport.status)) == "failed"


# ---- Verified data and your edits win ------------------------------------------------------------------------

def test_confirmed_facts_are_never_overwritten_and_conflicts_are_flagged(db, settings):
    seed_profile(db, skills=("java",), learning=())
    ps.save_fact(db, "employment", ps.EmploymentIn(company="Example Systems Pvt Ltd", title="Senior Software Engineer",
                                                   start_date=date(2024, 1, 15), description="My own words.",
                                                   status="confirmed"))
    java = db.scalar(select(Skill).where(Skill.normalized_name == "java"))
    ps.save_fact(db, "skills", ps.SkillIn(name="java", status="confirmed", evidence="Mine"), java.id)
    db.commit()
    imp = _import(db, settings)
    items = {it["key"]: it for it in imp.report["items"]}
    emp = db.scalar(select(Employment).where(Employment.company.like("Example Systems%")))
    assert emp.title == "Senior Software Engineer" and emp.description == "My own words.", "confirmed text untouched"
    assert items["employment.examplesystems.title"]["outcome"] == "conflict"
    assert items["employment.examplesystems.title"]["existing"] == "Senior Software Engineer"
    assert items["employment.examplesystems.dates"]["outcome"] == "unchanged", "same month: consistent"
    assert items["employment.examplesystems.bullet[0]"]["outcome"] == "suggestion"
    edu = db.scalar(select(Education).where(Education.degree == "B.Tech"))
    assert edu.grade is None and items["education.btech.exampleinstituteoftechnology.grade"]["outcome"] == "suggestion"
    assert db.scalar(select(func.count(Education.id))) == 2, "the matching degree is reused; Class XII is new"
    assert java.evidence == "Mine" and java.status == "confirmed"
    p = ps.get_profile(db)
    assert p.headline == "Backend engineer (synthetic test profile)" and p.confirmed_at is not None
    assert items["profile.headline"]["outcome"] == "conflict"
    assert items["profile.phone"]["outcome"] == "unchanged", "same number in another format is not a conflict"
    assert any("employment title" in c for c in hub.open_conflicts(db))


def test_apply_and_dismiss_resolve_conflicts(db, settings):
    seed_profile(db, skills=(), learning=())
    db.commit()
    _import(db, settings)
    headline = db.scalar(select(ProfileClaim).where(ProfileClaim.claim_key == "profile.headline"))
    assert headline.outcome == "conflict"
    hub.apply_claim(db, headline)
    db.commit()
    assert ps.get_profile(db).headline == "Backend Engineer" and headline.outcome == "applied"
    assert ps.get_profile(db).confirmed_at is None, "changed details need re-confirmation"
    phone = db.scalar(select(ProfileClaim).where(ProfileClaim.claim_key == "profile.phone"))
    assert phone.outcome == "unchanged"
    with pytest.raises(ValueError):
        hub.dismiss_claim(db, phone)
    ps.update_profile(db, ps.ProfileIn.model_validate({**{f: getattr(ps.get_profile(db), f) for f in ps.ProfileIn.model_fields},
                                                       "headline": "Platform engineer"}))
    db.commit()
    imp = _import(db, settings)
    assert {it["key"]: it for it in imp.report["items"]}["profile.headline"]["outcome"] == "conflict"
    hub.dismiss_claim(db, headline)
    db.commit()
    imp = _import(db, settings)
    assert {it["key"]: it for it in imp.report["items"]}["profile.headline"]["outcome"] == "dismissed", \
        "a dismissed value is not raised again for the same file"
    assert headline.state == "rejected" and ps.get_profile(db).headline == "Platform engineer"


def test_new_resume_version_updates_untouched_values_but_keeps_your_edits(db, settings):
    _import(db, settings)
    proj = db.scalar(select(Project))
    v2 = [ln.replace("100+ requests", "250+ requests") for ln in RESUME_LINES]
    imp = _import(db, settings, resume_pdf(v2), name="candidate-v2.pdf")
    items = {it["key"]: it for it in imp.report["items"]}
    assert items["project.widgettracker.description"]["outcome"] == "updated", "still the imported text: refreshed"
    assert "250+ requests" in proj.description
    assert db.scalar(select(func.count(ResumeVersion.id))) == 2 and db.scalar(select(func.count(Project.id))) == 1
    ps.save_fact(db, "projects", ps.ProjectIn.model_validate({**{f: getattr(proj, f) for f in ps.ProjectIn.model_fields},
                                                              "description": "My rewrite"}), proj.id)
    db.commit()
    v3 = [ln.replace("100+ requests", "300+ requests") for ln in RESUME_LINES]
    imp = _import(db, settings, resume_pdf(v3), name="candidate-v3.pdf")
    assert {it["key"]: it for it in imp.report["items"]}["project.widgettracker.description"]["outcome"] == "conflict"
    assert proj.description == "My rewrite", "your edit is kept"


def test_resume_version_registration_is_not_duplicated(db, settings):
    from jobpilot.services import resumes as rs

    master = rs.create_resume(db, "Master resume", "master", True)
    v = rs.import_version(db, settings, master, "cv.pdf", resume_pdf())
    rs.approve_version(db, v, acknowledged=True)
    db.commit()
    imp = _import(db, settings)
    assert imp.resume_version_id == v.id and db.scalar(select(func.count(ResumeVersion.id))) == 1
    assert v.status == "approved", "an existing approval is left as it was"
    assert {it["key"]: it for it in imp.report["items"]}["resume_version"]["outcome"] == "unchanged"


def test_migration_0008_is_safe(tmp_path):
    from alembic import command

    from jobpilot.config import Settings
    from jobpilot.db import alembic_config, make_engine, migrate

    settings = Settings(_env_file=None, data_dir=tmp_path / "d")
    settings.ensure_dirs()
    eng = make_engine(settings)
    cfg = alembic_config(settings)
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0007")
        conn.execute(text("INSERT INTO skills (name, normalized_name, status, created_at, updated_at) VALUES "
                          "('Java', 'java', 'confirmed', '2026-01-01', '2026-01-01')"))
    res = migrate(settings, eng)
    assert res["from"] == "0007" and res["backup"], "a backup is taken before migrating"
    with eng.connect() as conn:
        assert conn.execute(text("select name, status from skills")).fetchall() == [("Java", "confirmed")]
        assert conn.execute(text("select count(*) from profile_claims")).scalar() == 0
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0007")
    with eng.connect() as conn:
        tables = {r[0] for r in conn.execute(text("select name from sqlite_master where type='table'"))}
        assert "profile_claims" not in tables and "profile_imports" not in tables
        assert conn.execute(text("select count(*) from skills")).scalar() == 1
    eng.dispose()


# ---- Repository documentation ----------------------------------------------------------------------------------

def test_link_project_docs_references_without_copying(db, settings, tmp_path):
    _import(db, settings)
    repo = tmp_path / "widget-tracker"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("# Widget Tracker\n\nA small inventory demo (synthetic).\n\n## More\n")
    (repo / "docs" / "architecture.md").write_text("x")
    (repo / "secret.py").write_text("API_KEY = 'never read'")
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/test-candidate/widget-tracker.git\n')
    proj = db.scalar(select(Project))
    claims = {c.field + ":" + c.claim_key: c for c in hub.link_project_docs(db, proj, repo)}
    assert claims["repository_url:git.origin"].notes == "matches the project URL on the resume"
    assert claims["repository_description:readme.summary"].value == "A small inventory demo (synthetic)."
    assert claims["documentation:docs"].value == "architecture.md"
    assert all(c.source_kind == "repository" and c.state == "unverified" for c in claims.values())
    assert not any("never read" in (c.value or "") for c in claims.values())
    assert proj.description.startswith("Inventory Demo"), "the resume's wording is kept apart from the README's"


# ---- Web: hub page, API, import, resolution ----------------------------------------------------------------------

def test_hub_page_api_and_web_import(client, db):
    r = client.post("/hub/import", files={"file": ("candidate.pdf", resume_pdf(), "application/pdf")})
    assert r.status_code == 303 and "/hub/imports/" in r.headers["location"]
    assert client.get(r.headers["location"]).status_code == 200
    page = client.get("/hub").text
    for needle in ("Personal information hub", "Example Systems", "Widget Tracker", "Example Cloud Fundamentals",
                   "CGPA: 8.10/10", "resume claim", "Unconfirmed", "page 1 · EXPERIENCE"):
        assert needle in page, needle
    assert "Information hub" in client.get("/").text, "linked from the navigation"
    found = client.get("/hub", params={"q": "30%"}).text
    assert "Reduced build time by 30%" in found
    snap = client.get("/api/hub").json()
    assert snap["profile"]["full_name"] == "Test Candidate" and snap["profile"]["confirmed"] is False
    assert "java" in {s["canonical"] for s in snap["skills"]} and snap["unconfirmed_skills"]
    assert snap["profile"]["provenance"]["email"][0]["state"] == "resume_claim"
    rows = client.get("/api/hub/claims", params={"category": "employment"}).json()
    assert rows and all(r["category"] == "employment" for r in rows)
    # Re-importing the registered version from the hub is idempotent.
    vid = db.scalar(select(ResumeVersion.id))
    r = client.post("/hub/import", data={"version_id": str(vid)})
    assert "0+created" in r.headers["location"] and "0+updated" in r.headers["location"]
    # Confirming from the hub returns to the hub.
    proj = db.scalar(select(Project))
    r = client.post(f"/profile/projects/{proj.id}/status", data={"status": "confirmed", "next": "/hub#projects"})
    assert r.headers["location"].startswith("/hub")
    r = client.post(f"/profile/projects/{proj.id}/status", data={"status": "confirmed", "next": "https://evil.example"})
    assert r.headers["location"].startswith("/profile")
    db.expire_all()
    assert client.get("/hub").status_code == 200


def test_hub_conflict_actions_over_http(client, db):
    seed_profile(db, skills=(), learning=())
    db.commit()
    client.post("/hub/import", files={"file": ("candidate.pdf", resume_pdf(), "application/pdf")})
    claim = db.scalar(select(ProfileClaim).where(ProfileClaim.claim_key == "profile.headline"))
    assert "Backend engineer (synthetic test profile)" in client.get("/hub").text
    r = client.post(f"/hub/claims/{claim.id}/dismiss")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    r = client.post(f"/hub/claims/{claim.id}/apply")
    assert "err=" in r.headers["location"], "a dismissed value cannot be applied by accident"
    assert client.post("/hub/claims/999999/apply").status_code == 404
    r = client.post("/hub/import", files={"file": ("bad.pdf", b"%PDF-1.4 broken", "application/pdf")})
    assert "err=" in r.headers["location"]


# ---- Integration with matching, tailoring, preparation, queue and interviews ----------------------------------------

def _job(db, desc="Requirements:\n- Java\n- Kafka\n- PostgreSQL"):
    from jobpilot.services import jobs as job_svc

    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", company_name="Examplecorp",
                                                        location="Bengaluru", description_text=desc))
    return job


def test_matching_uses_imported_skills_only_once_confirmed(db, settings):
    from jobpilot.services import matching

    _import(db, settings)
    job = _job(db)
    assert "kafka" not in job.match_details["skills"]["have"], "an imported skill is a claim, not a match"
    kafka = db.scalar(select(Skill).where(Skill.normalized_name == "kafka"))
    ps.set_fact_status(db, "skills", kafka.id, "confirmed")
    matching.match_job(db, job)
    assert "kafka" in job.match_details["skills"]["have"]


def test_resume_tailoring_refuses_unconfirmed_imported_facts(db, settings):
    from jobpilot.services import resumes as rs

    _import(db, settings)
    ps.confirm_profile(db)
    proj = db.scalar(select(Project))
    with pytest.raises(rs.ResumeError, match="Only confirmed facts"):
        rs.build_resume_html(db, {"projects": [proj.id]})
    ps.set_fact_status(db, "projects", proj.id, "confirmed")
    html, _ = rs.build_resume_html(db, {"projects": [proj.id]})
    assert "Widget Tracker" in html and "100+ requests per second" in html


def test_application_preparation_uses_approved_answers_and_flags_conflicts(db, settings):
    from jobpilot.services import answers as answer_svc
    from jobpilot.services import applications as apps

    seed_profile(db, skills=("java",), learning=())
    ps.update_profile(db, ps.ProfileIn.model_validate({**{f: getattr(ps.get_profile(db), f) for f in ps.ProfileIn.model_fields},
                                                       "notice_period_days": 30}))
    db.commit()
    _import(db, settings)
    draft = db.scalar(select(AnswerBankEntry).where(AnswerBankEntry.question == "What is your notice period?"))
    assert draft.answer == "30 days" and not draft.approved
    assert answer_svc.suggest(db, "What is your notice period?", None) is None, "drafts are never suggested"
    answer_svc.save_entry(db, draft.question, draft.answer, draft.category, approved=True, entry_id=draft.id)
    assert answer_svc.suggest(db, "What is your notice period?", None)["entry"].id == draft.id
    app = apps.create(db, _job(db))
    snap = apps.prepare_snapshot(db, app)
    assert any("headline" in c for c in snap["missing"]["profile_conflicts"])


def test_review_queue_and_validation_surface_hub_problems(db, settings):
    from jobpilot.services import queue

    seed_profile(db, skills=(), learning=())
    db.commit()
    _import(db, settings)
    items = [i for i in queue.build(db) if i["category"] == "missing_profile_info"]
    assert any(i["link"] == "/hub#attention" for i in items)
    issues = {i["area"]: i["message"] for i in hub.validation_issues(db)}
    assert "conflicts" in issues and "preferences" in issues
    assert any("Kind of work is not recorded for Software Engineer" in i["message"] for i in hub.validation_issues(db))


def test_prep_pack_talks_about_confirmed_projects_numbers(db, settings):
    from jobpilot.services import interviews as iv_svc

    _import(db, settings)
    proj = db.scalar(select(Project))
    job = _job(db)
    app = Application(job_id=job.id, status="applied", applied_at=utcnow())
    db.add(app)
    db.flush()
    iv = iv_svc.save(db, iv_svc.InterviewIn(application_id=app.id, stage="technical"))
    assert iv_svc.generate_prep_pack(db, iv).content["project_talking_points"] == [], "unconfirmed: not used"
    ps.set_fact_status(db, "projects", proj.id, "confirmed")
    points = iv_svc.generate_prep_pack(db, iv).content["project_talking_points"]
    assert points and "100+ requests per second" in points[0]


def test_cli_hub_import_and_report(settings, monkeypatch, tmp_path, capsys):
    import json

    from jobpilot import cli

    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(settings.data_dir))
    assert cli.main(["init"]) == 0
    f = tmp_path / "candidate.pdf"
    f.write_bytes(resume_pdf())
    capsys.readouterr()
    assert cli.main(["hub", "import", "--file", str(f)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "completed" and out["counts"]["created"] > 20 and out["backup"]
    assert cli.main(["hub", "import", "--version", str(out["resume_version_id"])]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["counts"]["created"] == 0 and again["counts"]["updated"] == 0
    assert cli.main(["hub", "report"]) == 0
    assert json.loads(capsys.readouterr().out)["import_id"] == again["import_id"]
    assert cli.main(["hub", "import"]) == 2
