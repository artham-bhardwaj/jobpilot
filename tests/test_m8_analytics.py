"""Milestone 8 — Stage I: analytics reports, CSV exports, dashboard pipeline (offline; synthetic)."""

from __future__ import annotations

import csv
import io
import json

from sqlalchemy import select

from jobpilot.models import Resume
from jobpilot.services import analytics
from jobpilot.services import jobs as job_svc
from jobpilot.services import resumes as rs
from tests.helpers import make_http, make_pdf_bytes, seed_profile

GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def _board(n):
    return json.dumps({"jobs": [{"id": i, "title": f"Java Backend Engineer {i}", "location": {"name": "Bengaluru"},
                                 "absolute_url": f"https://boards.greenhouse.io/examplecorp/jobs/{i}",
                                 "content": "Requirements: Java, Spring Boot, SQL, Kafka. 1+ years of experience."}
                                for i in range(1, n + 1)]})


def test_collection_resume_selection_and_skill_gap_reports(db, settings):
    seed_profile(db)
    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                             config={"board_token": "examplecorp"}))
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}))
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(3))}), dry_run=True)
    # The same posting also listed manually: a duplicate sighting.
    job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer 1", company_name="Examplecorp",
                                               location="Bengaluru", apply_url="https://boards.greenhouse.io/examplecorp/jobs/1"))
    s = analytics.summary(db)
    gh = next(r for r in s["collection"] if r["source"] == "Examplecorp")
    assert gh["runs"] == 1 and gh["new"] == 3 and gh["fetched"] == 3, "dry runs excluded"
    from jobpilot.models import Job

    dup = db.scalar(select(Job).where(Job.external_id == "1"))
    assert len(dup.sightings) == 2 and len(list(db.scalars(select(Job)))) == 3, "manual copy deduplicated, provenance kept"
    sel = s["resume_selection"]
    assert sel["jobs_considered"] >= 1 and sel["by_category"]["java_backend"]["missing"] == sel["by_category"]["java_backend"]["jobs"]
    rs.create_standard_slots(db)
    r = db.scalar(select(Resume).where(Resume.category == "java_backend"))
    rs.approve_version(db, rs.import_version(db, settings, r, "j.pdf", make_pdf_bytes(["Test Candidate", "Java"])))
    db.flush()
    db.expire_all()
    sel = analytics.summary(db)["resume_selection"]["by_category"]["java_backend"]
    assert sel["exact"] == sel["jobs"] and sel["missing"] == 0
    gaps = analytics.summary(db)["skill_gaps"]
    assert gaps["jobs_analyzed"] >= 3 and "kafka" in [g["skill"] for g in gaps["top"]]
    assert analytics.summary(db)["review"], "review queue counts reported"


def test_duplicate_share_uses_minimum_sample(db, settings):
    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", config={"board_token": "examplecorp"}))
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(2))}))
    row = analytics.summary(db)["collection"][0]
    assert row["duplicate_share"]["value"] is None and "insufficient" in row["duplicate_share"]["note"]


def test_new_csv_exports_and_formula_guard(db, settings):
    seed_profile(db)
    job_svc.save_source(db, job_svc.SourceIn(name="=cmd|' /C calc'!A0", kind="greenhouse",
                                             config={"board_token": "examplecorp"}))
    job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(2))}))
    sources = list(csv.reader(io.StringIO(analytics.export_csv(db, "sources"))))
    assert sources[0][:3] == ["source", "kind", "runs"] and sources[1][0].startswith("'=")
    gaps = list(csv.reader(io.StringIO(analytics.export_csv(db, "skill_gaps"))))
    assert gaps[0][0] == "skill" and any(r[0] == "kafka" for r in gaps[1:])


def test_analytics_and_dashboard_pages(client, db, settings):
    seed_profile(db)
    job_svc.add_manual(db, job_svc.ManualJobIn(title="Java Backend Engineer", company_name="Examplecorp",
                                               description_text="Requirements: Java, Spring Boot, SQL"))
    db.commit()
    page = client.get("/analytics").text
    for h in ("Collection by source", "Awaiting review", "Resume selection by role category", "Common skill gaps"):
        assert h in page, h
    for kind in ("sources", "skill_gaps"):
        r = client.get(f"/analytics/export/{kind}.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    home = client.get("/").text
    assert "Pipeline" in home and "Applied (with evidence)" in home
