"""Milestone 34: job-specific resumes generated locally from the approved Master resume's LaTeX source.

Synthetic candidate and template (same structure as a real one). Compilation uses the local LaTeX compiler when it is
installed (those tests are skipped otherwise); failures use a fake compiler. Nothing is submitted or sent."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from jobpilot.models import Application, Employment, Notification, Project, ResumeVersion, Skill
from jobpilot.models.tailored import TailoredResume
from jobpilot.services import applications as apps
from jobpilot.services import notifications, settings_store, tailored
from tests.helpers import make_pdf_bytes, seed_profile
from tests.apply_fixtures import job

needs_tex = pytest.mark.skipif(tailored.compiler() is None, reason="no local LaTeX compiler")

TEX = r"""\documentclass[a4paper,10pt]{article}
\usepackage[margin=0.6in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\newcommand{\isep}{-2pt}
\newcommand{\resheading}[1]{{\small \textbf{#1}}\vspace{2pt}}

\begin{document}

\begin{center}
{\Large \textbf{Test Candidate}}\\[2pt]
\href{mailto:test.candidate@example.com}{test.candidate@example.com}
\enspace$\cdot$\enspace +91 90000 00000
\end{center}

%-----------------------------------------------------------
\resheading{\textbf{EXPERIENCE}}

\begin{itemize}\itemsep \isep

\item
\textbf{Software Engineer, Synthetic Corp}
\hfill \textit{Jan 2024 -- Present}
\\[-0.55cm]

\begin{itemize}\itemsep \isep

\item Built \textbf{Java} services handling \textbf{10+ REST APIs} (synthetic).

\item Wrote Python scripts for data checks (synthetic).

\item Tuned \textbf{Kafka} consumers for event pipelines, cutting lag by \textbf{30\%} (synthetic).

\end{itemize}
\end{itemize}

%-----------------------------------------------------------
\resheading{\textbf{TECHNICAL SKILLS}}

\begin{itemize}\itemsep \isep

\item \noindent \textbf{Languages:} Python, SQL, Java

\item \noindent \textbf{Systems:} Docker, Kafka

\end{itemize}

%-----------------------------------------------------------
\resheading{\textbf{PROJECTS}}

\begin{itemize}

\item
\textbf{Ledger -- Python Tool}
\hfill \textit{2025}
\\[-0.55cm]

\begin{itemize}\itemsep \isep

\item Built a Python ledger tool with SQL reports (synthetic).

\end{itemize}

\item
\textbf{Streamer -- Kafka Demo}
\hfill \textit{2025}
\\[-0.55cm]

\begin{itemize}\itemsep \isep

\item Built a Kafka streaming demo in Java (synthetic).

\end{itemize}

\end{itemize}

%-----------------------------------------------------------
\resheading{\textbf{EDUCATION}}

\begin{itemize}\itemsep \isep

\item \noindent \textbf{B.Tech, Computer Science} --- Example Institute of Technology \hfill \textit{2020 -- 2024}

\end{itemize}

\end{document}
"""

EMP_LINES = ["Built Java services handling 10+ REST APIs (synthetic).", "Wrote Python scripts for data checks (synthetic).",
             "Tuned Kafka consumers for event pipelines, cutting lag by 30% (synthetic)."]
DESCRIPTION = ("We are hiring a backend engineer to build event pipelines. Requirements: Java and Kafka in production "
               "services; Docker is a plus. You will design APIs, operate streaming consumers, and improve "
               "reliability across services. " * 3)


def _facts(db):
    from jobpilot.services import profile as ps

    seed_profile(db, skills=("java", "python", "sql", "kafka", "docker"), learning=())
    emp = db.scalar(select(Employment))
    emp.description = "\n".join(EMP_LINES)
    ps.save_fact(db, "projects", ps.ProjectIn(name="Ledger", description="Python Tool\nBuilt a Python ledger tool with "
                                              "SQL reports (synthetic).", technologies=["Python"], status="confirmed"))
    ps.save_fact(db, "projects", ps.ProjectIn(name="Streamer", description="Kafka Demo\nBuilt a Kafka streaming demo in "
                                              "Java (synthetic).", technologies=["Java", "Kafka"], status="confirmed"))
    db.flush()


@pytest.fixture
def tworld(db, settings, tmp_path):
    """Facts, an approved Master resume compiled from TEX, the registered template, and an eligible job."""
    from jobpilot.services import resumes as rs

    _facts(db)
    master = rs.create_resume(db, "Master resume", "master", True)
    v = rs.import_version(db, settings, master, "master.pdf", tailored.compile_pdf(TEX))
    rs.approve_version(db, v, acknowledged=True)
    src = tmp_path / "resume.tex"
    src.write_text(TEX)
    tailored.register_template(db, settings, src)
    j = _job(db, "https://job-boards.greenhouse.io/x/jobs/1")
    db.commit()
    return SimpleNamespace(settings=settings, master=v, job=j, src=src)


def _job(db, url, **kw):
    j = job(db, url, title=kw.pop("title", "Backend Engineer, Streaming"), company=kw.pop("company", "Examplecorp"), **kw)
    j.description_text = DESCRIPTION
    j.match_details = {"skills": {"mandatory": ["java", "kafka"], "preferred": ["docker", "go"], "mentioned": []}}
    db.commit()
    return j


# ---- parsing and tailoring (no compiler needed) ----------------------------------------------------------------

def test_template_round_trips_byte_for_byte():
    doc = tailored.parse(TEX)
    assert doc.render() == TEX
    assert [s.heading for s in doc.sections] == ["EXPERIENCE", "TECHNICAL SKILLS", "PROJECTS", "EDUCATION"]
    assert tailored.plain(r"Cut lag by \textbf{30\%} --- \href{https://x}{GitHub}") == "Cut lag by 30% — GitHub"


def test_tailoring_only_reorders_confirmed_content(db, settings):
    _facts(db)
    j = _job(db, "https://example.com/1")
    res = tailored.tailor(db, settings, j, TEX)
    d = res["decisions"]
    exp = d["sections"]["EXPERIENCE"][0]["order"]
    assert exp[-1].startswith("Wrote Python scripts"), "bullets naming the job's skills come first"
    assert d["sections"]["PROJECTS order"] == {"reordered": True, "order": ["Streamer", "Ledger"]}
    assert d["sections"]["TECHNICAL SKILLS"][0]["order"] == ["Java", "Python", "SQL"]
    assert d["not_claimed"] == ["go"], "a required skill you don't have is reported, never added"
    out = tailored.plain(res["tex"])
    assert "Go" not in out.split() and sorted(tailored.alnum(out)) == sorted(tailored.alnum(tailored.plain(TEX))), \
        "the same words, only re-ordered"
    assert all(m["fact"] for m in d["mapping"]) and not d["excluded"]


def test_unconfirmed_and_unsupported_content_is_left_out(db, settings):
    _facts(db)
    db.scalar(select(Project).where(Project.name == "Ledger")).status = "unverified"
    fabricated = TEX.replace(r"\item Wrote Python scripts", r"\item Reduced cloud cost by \textbf{90\%} (synthetic)."
                             "\n\n\\item Wrote Python scripts")
    res = tailored.tailor(db, settings, _job(db, "https://example.com/1"), fabricated)
    excluded = {e["text"]: e["reason"] for e in res["excluded"]}
    assert excluded["Reduced cloud cost by 90% (synthetic)."] == "not found in any confirmed profile fact"
    assert any("Ledger" in t for t in excluded), "an unconfirmed project is left out"
    assert "90%" not in tailored.plain(res["tex"]) and "Ledger" not in res["tex"]


def test_employment_dates_must_match_the_confirmed_record(db, settings):
    _facts(db)
    db.scalar(select(Employment)).start_date = date(2023, 6, 1)
    with pytest.raises(tailored.TailorError, match="dates"):
        tailored.tailor(db, settings, _job(db, "https://example.com/1"), TEX)


def test_missing_template_and_any_job_on_request(db, settings):
    _facts(db)
    j = _job(db, "https://example.com/1")
    with pytest.raises(tailored.TailorError, match="No resume template registered"):
        tailored.load_template(db, settings)
    tr = tailored.generate(db, settings, j)
    assert tr.status == "failed" and "No resume template" in tr.error
    # You choose the job: low scores, ineligible or short postings are allowed, with a note on the page.
    for change, note in (({"eligibility": "ineligible"}, "ineligible"), ({"hard_conflict": True}, "ineligible"),
                         ({"description_text": "short"}, "short"), ({"relevance_score": 10}, "ready"),
                         ({"status": "expired"}, "expired")):
        other = _job(db, f"https://example.com/{list(change)[0]}-{list(change.values())[0]}")
        for k, v in change.items():
            setattr(other, k, v)
        ok, why = tailored.eligible(db, other)
        assert ok and note in why, (change, why)


# ---- generation, validation, storage ------------------------------------------------------------------------------

@needs_tex
def test_generate_validate_store_and_reuse(tworld, db):
    s, j = tworld.settings, tworld.job
    master_bytes = (s.files_dir / tworld.master.file_path).read_bytes()
    tr = tailored.generate(db, s, j)
    db.commit()
    assert tr.status == "valid" and tr.compile_status == "ok" and tr.validation_status == "passed", tr.error
    assert all(c["ok"] for c in tr.validation["checks"].values())
    assert tr.pdf_path == f"tailored_resumes/examplecorp_{j.id}_{tr.content_sha256[:12]}.pdf"
    pdf = (s.files_dir / tr.pdf_path).read_bytes()
    assert hashlib.sha256(pdf).hexdigest() == tr.pdf_sha256 and tr.source_version_id == tworld.master.id
    text = __import__("jobpilot.services.resumes", fromlist=["x"]).extract_text("x.pdf", pdf)["text"]
    assert text.index("Streamer") < text.index("Ledger"), "re-ordered for the job"
    assert (s.files_dir / tworld.master.file_path).read_bytes() == master_bytes, "the Master resume is untouched"
    db.refresh(tworld.master)
    assert tworld.master.status == "approved" and tworld.master.sha256 == hashlib.sha256(master_bytes).hexdigest()

    again = tailored.generate(db, s, j)
    assert again.id == tr.id, "unchanged inputs reuse the artifact"
    assert db.query(TailoredResume).count() == 1
    forced = tailored.generate(db, s, j, force=True)
    db.commit()
    assert forced.id != tr.id and forced.pdf_sha256 == tr.pdf_sha256, "deterministic: same source, same PDF"
    db.refresh(tr)
    assert tr.status == "superseded"


@needs_tex
def test_changed_facts_create_a_new_version(tworld, db):
    s, j = tworld.settings, tworld.job
    first = tailored.generate(db, s, j)
    db.commit()
    db.scalar(select(Skill).where(Skill.normalized_name == "docker")).years = 1.0  # a confirmed fact changed
    db.commit()
    second = tailored.generate(db, s, j)
    db.commit()
    assert second.id != first.id and second.facts_sha256 != first.facts_sha256
    db.refresh(first)
    assert first.status == "superseded" and second.status == "valid"


@needs_tex
def test_unconfirmed_project_is_left_out_of_the_pdf_and_still_validates(tworld, db):
    db.scalar(select(Project).where(Project.name == "Ledger")).status = "unverified"
    db.commit()
    tr = tailored.generate(db, tworld.settings, tworld.job)
    assert tr.status == "valid", tr.error
    pdf_text = __import__("jobpilot.services.resumes", fromlist=["x"]).extract_text(
        "x.pdf", (tworld.settings.files_dir / tr.pdf_path).read_bytes())["text"]
    assert "Ledger" not in pdf_text and "Streamer" in pdf_text


@needs_tex
def test_template_must_be_the_approved_resume(tworld, db, tmp_path):
    other = tmp_path / "other.tex"
    other.write_text(TEX.replace("10+ REST APIs", "50+ REST APIs"))
    with pytest.raises(tailored.TailorError, match="differs from the approved Master"):
        tailored.register_template(db, tworld.settings, other)
    path = tworld.settings.files_dir / tailored.template_info(db)["path"]
    path.chmod(0o600)
    path.write_text(TEX + "% changed")
    with pytest.raises(tailored.TailorError, match="checksum"):
        tailored.load_template(db, tworld.settings)


@pytest.mark.parametrize("fake,check", [
    (b"", "file_exists"),
    (make_pdf_bytes(["Test Candidate {{phone}} TODO"]), "no_placeholders"),
    (make_pdf_bytes(["Test Candidate", "LaTeX Error: File not found"]), "no_latex_errors"),
])
@needs_tex
def test_invalid_pdfs_are_rejected(tworld, db, fake, check):
    tr = tailored.generate(db, tworld.settings, tworld.job, compile_fn=lambda tex, timeout: fake)
    assert tr.status == "failed" and tr.validation_status == "failed"
    assert not tr.validation["checks"][check]["ok"]
    assert tailored.latest(db, tworld.job.id, "valid") is None


@needs_tex
def test_page_target_and_missing_content(tworld, db):
    settings_store.put(db, "tailored_resumes", {**tailored.cfg(db), "page_target": 1})
    two_pages = tailored.compile_pdf(TEX.replace(r"\begin{document}", "\\begin{document}\n\\newpage\\null\\newpage"))
    tr = tailored.generate(db, tworld.settings, tworld.job, compile_fn=lambda tex, timeout: two_pages)
    assert not tr.validation["checks"]["page_count"]["ok"] and not tr.validation["checks"]["no_blank_pages"]["ok"]
    clipped = tailored.compile_pdf(TEX.replace("Built a Kafka streaming demo", "Built a"))
    tr = tailored.generate(db, tworld.settings, tworld.job, force=True, compile_fn=lambda tex, timeout: clipped)
    assert not tr.validation["checks"]["all_content_present"]["ok"]


def test_compiler_missing_and_compile_failure(db, settings, tmp_path, monkeypatch):
    _facts(db)
    j = _job(db, "https://example.com/1")
    monkeypatch.setattr(tailored, "load_template", lambda s, st: (TEX, {"sha256": "t", "source_version_id": None}, SimpleNamespace(id=None)))
    monkeypatch.setattr(tailored, "compiler", lambda settings=None: None)
    tr = tailored.generate(db, settings, j)
    assert tr.status == "failed" and tr.compile_status == "unavailable" and "sudo apt install" in tr.error

    def broken(tex, timeout):
        raise tailored.TailorError("LaTeX compilation failed: ! Undefined control sequence.")
    tr = tailored.generate(db, settings, j, force=True, compile_fn=broken)
    assert tr.compile_status == "failed" and "Undefined control sequence" in tr.error

    class Proc:
        returncode = 1
        stdout = b"! LaTeX Error: File `missing.sty' not found."
    monkeypatch.setattr(tailored, "compiler", lambda settings=None: ["pdflatex"])
    with pytest.raises(tailored.TailorError, match="missing.sty"):
        tailored.compile_pdf(TEX, runner=lambda *a, **k: Proc())


# ---- attaching ----------------------------------------------------------------------------------------------------

def _app(db, j, version):
    a = apps.create(db, j, status="shortlisted")
    a.resume_version_id = version.id
    db.commit()
    return a


@needs_tex
def test_attachment_uses_only_the_jobs_valid_current_resume(tworld, db):
    s, j = tworld.settings, tworld.job
    master_path = s.files_dir / tworld.master.file_path
    a = _app(db, j, tworld.master)
    before = {c: getattr(a, c) for c in ("status", "resume_version_id", "applied_at", "apply_url")}
    assert tailored.attachment_for(db, s, a) == (master_path, "approved resume"), "nothing generated yet"
    tr = tailored.generate(db, s, j)
    db.commit()
    path, label = tailored.attachment_for(db, s, a)
    assert path == s.files_dir / tr.pdf_path and label == f"tailored resume #{tr.id}"
    assert tr.attached_application_id == a.id and tr.attached_at
    db.refresh(a)
    assert {c: getattr(a, c) for c in before} == before, "the application itself is unchanged"

    other_job = _job(db, "https://job-boards.greenhouse.io/x/jobs/2", title="Another Role")
    b = _app(db, other_job, tworld.master)
    assert tailored.attachment_for(db, s, b)[0] == master_path, "never another job's resume"
    assert "another job" in tailored.attach_problem(db, s, tr, b)

    j.description_text = DESCRIPTION + " Now also Rust."
    db.commit()
    assert "changed since" in tailored.attachment_for(db, s, a)[1], "stale: the job changed"
    j.description_text = DESCRIPTION
    db.commit()
    (s.files_dir / tr.pdf_path).write_bytes(b"%PDF-1.4 tampered")
    assert tailored.attachment_for(db, s, a)[0] == master_path and "missing or changed" in tailored.attach_problem(db, s, tr, a)
    tr.status = "failed"
    db.commit()
    assert tailored.attachment_for(db, s, a)[0] == master_path


@needs_tex
def test_attachment_respects_the_applications_own_resume_choice(tworld, db):
    from jobpilot.services import resumes as rs

    s = tworld.settings
    tailored.generate(db, s, tworld.job)
    other = rs.create_resume(db, "Java backend", "java_backend")
    ov = rs.import_version(db, s, other, "java.pdf", make_pdf_bytes(["Test Candidate", "Java"]))
    rs.approve_version(db, ov, acknowledged=True)
    a = _app(db, tworld.job, ov)
    assert tailored.attachment_for(db, s, a)[0] == s.files_dir / ov.file_path
    settings_store.put(db, "tailored_resumes", {**tailored.cfg(db), "attach": False})
    a.resume_version_id = tworld.master.id
    assert tailored.attachment_for(db, s, a)[1] == "approved resume", "attach can be turned off"


# ---- on request only, CLI ------------------------------------------------------------------------------

def test_resumes_are_never_generated_in_the_background_or_announced(db, settings):
    from jobpilot.services import scheduler

    assert "tailor_resumes" not in scheduler.TASKS and not hasattr(tailored, "run")
    _facts(db)
    notifications.set_state(db, notifications.BASELINE_KEY, {"at": "2026-01-01T00:00:00", "events": 0})
    tailored.generate(db, settings, _job(db, "https://example.com/1"))  # no template: fails
    notifications.scan(db, settings)
    assert db.query(Notification).filter(Notification.kind.like("tailored%")).count() == 0, "shown on the job page"


@needs_tex
def test_cli_exit_codes(tworld, db, monkeypatch, capsys):
    from jobpilot import cli, cli_resume

    engine = db.get_bind()
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(cli, "_ctx", lambda: (tworld.settings, engine, sessionmaker(bind=engine)))
    run = lambda action, job_id=None, **kw: cli_resume.cmd_resume(SimpleNamespace(action=action, job=job_id, file=kw.get("file"), limit=10))  # noqa: E731
    assert run("generate", 99999) == 1
    assert run("preview", tworld.job.id) == 0
    assert run("generate", tworld.job.id) == 0
    capsys.readouterr()
    assert run("path", tworld.job.id) == 0
    path = capsys.readouterr().out.strip()
    assert path.endswith(".pdf") and f"_{tworld.job.id}_" in path
    assert run("validate", tworld.job.id) == 0
    with open(path, "ab") as f:
        f.write(b"\n% tampered")
    assert run("validate", tworld.job.id) == 3
    assert run("path", tworld.job.id) == 3
    capsys.readouterr()
    assert run("list") == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["job_id"] == tworld.job.id and row["status"] == "failed", "the tampered file failed re-validation"


@needs_tex
def test_dashboard_panel_and_download(tworld, client, db):
    r = client.get(f"/jobs/{tworld.job.id}")
    assert r.status_code == 200 and "Create resume for this job" in r.text
    r = client.post(f"/jobs/{tworld.job.id}/tailored-resume", follow_redirects=False)
    assert "ready" in r.headers["location"]
    db.expire_all()
    tr = tailored.latest(db, tworld.job.id, "valid")
    page = client.get(f"/jobs/{tworld.job.id}").text
    assert "tailoring decisions" in page and f"/tailored-resumes/{tr.id}/download" in page
    pdf = client.get(f"/tailored-resumes/{tr.id}/download")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


# ---- education years follow the confirmed records ----------------------------------------------------------------

CLASS12 = "\\item \\noindent \\textbf{Class XII (Science)} --- CBSE \\hfill \\textit{2021}\\\\\nScore: 95\\%\n\n"
TEX12 = TEX.replace("\\end{itemize}\n\n\\end{document}", CLASS12 + "\\end{itemize}\n\n\\end{document}")


def _class12(db, status="confirmed", end=date(2020, 5, 12)):
    from jobpilot.services import profile as ps

    ps.save_fact(db, "education", ps.EducationIn(institution="CBSE", degree="Class XII", field_of_study="Science",
                                                 start_date=date(2019, 4, 10), end_date=end, grade="Score: 95%",
                                                 status=status))
    db.flush()


def test_education_year_comes_from_the_confirmed_record(db, settings):
    assert TEX12 != TEX and tailored.parse(TEX12).render() == TEX12
    _facts(db)
    _class12(db)
    res = tailored.tailor(db, settings, _job(db, "https://example.com/1"), TEX12)
    d = res["decisions"]
    assert d["corrections"] == [{"section": "EDUCATION", "entry": "Class XII (Science) — CBSE 2021 Score: 95%",
                                 "from": "2021", "to": "2020", "fact": d["corrections"][0]["fact"]}]
    assert res["corrections"] == [("2021", "2020")]
    assert "\\textit{2020}" in res["tex"] and "\\textit{2021}" not in res["tex"]
    assert "Class XII (Science) — CBSE 2020 Score: 95%" in res["expected"]
    assert not any("CBSE" in w for w in d["warnings"]), "no discrepancy warning once the record decides"
    assert any("Example Institute" in w and "can't be checked" in w for w in d["warnings"])
    assert "2020 – 2024" in tailored.plain(res["tex"]), "an entry without confirmed dates is copied unchanged"


def test_unconfirmed_education_never_changes_the_resume(db, settings):
    _facts(db)
    _class12(db, status="unverified")
    res = tailored.tailor(db, settings, _job(db, "https://example.com/1"), TEX12)
    assert res["corrections"] == [] and "\\textit{2021}" in res["tex"]
    assert any("no confirmed education record" in w for w in res["decisions"]["warnings"])


def test_education_date_ranges_follow_the_record(db, settings):
    from jobpilot.models import Education

    _facts(db)
    edu = db.scalar(select(Education).where(Education.institution == "Example Institute of Technology"))
    edu.start_date, edu.end_date = date(2020, 8, 1), date(2024, 6, 30)
    res = tailored.tailor(db, settings, _job(db, "https://example.com/1"), TEX)
    assert res["corrections"] == [], "already right: nothing changes"
    edu.end_date = date(2025, 5, 31)
    res = tailored.tailor(db, settings, _job(db, "https://example.com/2"), TEX)
    assert res["corrections"] == [("2020 – 2024", "2020 – 2025")] and "\\textit{2020 -- 2025}" in res["tex"]


@needs_tex
def test_generated_pdf_shows_the_confirmed_year_and_validates(db, settings, tmp_path):
    from jobpilot.services import resumes as rs

    _facts(db)
    _class12(db)
    master = rs.create_resume(db, "Master resume", "master", True)
    v = rs.import_version(db, settings, master, "master.pdf", tailored.compile_pdf(TEX12))
    rs.approve_version(db, v, acknowledged=True)
    src = tmp_path / "resume.tex"
    src.write_text(TEX12)
    tailored.register_template(db, settings, src)
    master_bytes = (settings.files_dir / v.file_path).read_bytes()
    tr = tailored.generate(db, settings, _job(db, "https://job-boards.greenhouse.io/x/jobs/1"))
    assert tr.status == "valid", tr.error
    assert tr.decisions["corrections"][0]["to"] == "2020"
    from jobpilot.services.resumes import extract_text

    text = extract_text("x.pdf", (settings.files_dir / tr.pdf_path).read_bytes())["text"]
    assert "CBSE" in text and "2020" in text.split("CBSE", 1)[1][:80] and "2021" not in text.split("CBSE", 1)[1][:80]
    assert (settings.files_dir / v.file_path).read_bytes() == master_bytes and src.read_text() == TEX12
    # a PDF still showing the old year fails: the content check allows exactly the recorded correction
    old_year_pdf = tailored.compile_pdf(TEX12)
    bad = tailored.generate(db, settings, tr.job, force=True, compile_fn=lambda tex, timeout: old_year_pdf)
    assert bad.status == "failed" and not bad.validation["checks"]["same_content_as_master"]["ok"]


@needs_tex
def test_resumes_made_before_the_rule_are_stale_and_regenerated(tworld, db, monkeypatch):
    s, j = tworld.settings, tworld.job
    a = _app(db, j, tworld.master)
    old = tailored.generate(db, s, j)
    db.commit()
    monkeypatch.setattr(tailored, "PIPELINE_VERSION", "next")
    assert "changed since" in tailored.attach_problem(db, s, old, a), "a stale artifact is never attached"
    new = tailored.generate(db, s, j)
    db.commit()
    db.refresh(old)
    assert new.id != old.id and old.status == "superseded" and new.status == "valid"
    assert (s.files_dir / old.pdf_path).is_file(), "the earlier version's file is kept"


@needs_tex
def test_new_master_version_outdates_tailored_resumes(tworld, db, tmp_path):
    from jobpilot.services import resumes as rs

    s = tworld.settings
    a = _app(db, tworld.job, tworld.master)
    old = tailored.generate(db, s, tworld.job)
    db.commit()
    v1_bytes = (s.files_dir / tworld.master.file_path).read_bytes()
    tex2 = TEX.replace("\\textit{2020 -- 2024}", "\\textit{2020 -- 2025}")
    v2 = rs.import_version(db, s, tworld.master.resume, "master_v2.pdf", tailored.compile_pdf(tex2))
    assert v2.version_no == 2 and v2.status == "draft"
    rs.approve_version(db, v2, acknowledged=True)
    with pytest.raises(tailored.TailorError, match="no longer the approved one"):
        tailored.load_template(db, s)  # nothing is generated from v1's source once v2 is approved
    src2 = tmp_path / "v2.tex"
    src2.write_text(tex2)
    with pytest.raises(tailored.TailorError, match="differs"):
        tailored.register_template(db, s, tworld.src)  # v1's source no longer matches the approved Master
    info = tailored.register_template(db, s, src2)
    db.commit()
    assert info["source_version_id"] == v2.id and info["outdated_tailored_resumes"] == 1
    db.refresh(old)
    assert old.status == "outdated" and (s.files_dir / old.pdf_path).is_file()
    db.refresh(tworld.master)
    assert tworld.master.status == "approved" and (s.files_dir / tworld.master.file_path).read_bytes() == v1_bytes
    assert tailored.attachment_for(db, s, a)[0] == s.files_dir / tworld.master.file_path, \
        "the application keeps the approved v1 it was prepared with"
    new = tailored.generate(db, s, tworld.job)
    assert new.status == "valid" and new.source_version_id == v2.id
    assert tailored.attach_problem(db, s, new, a) == \
        "the application uses a different resume than the one it was made from"
