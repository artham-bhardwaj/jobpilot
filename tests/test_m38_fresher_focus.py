"""Jobs page focus: fresher / SDE-1 roles in India, remote or Dubai, in your role categories."""

from __future__ import annotations

import pytest

from jobpilot.services import jobs as job_svc
from jobpilot.services import levels
from jobpilot.sources.base import JobData


@pytest.mark.parametrize("title,years,expected", [
    ("Software Development Engineer I", None, "fit"),
    ("SDE-1 Backend", None, "fit"),
    ("Junior Java Developer", None, "fit"),
    ("Graduate Software Engineer", None, "fit"),
    ("Software Engineer", 0.0, "fit"),
    ("Software Engineer", 2.0, "fit"),
    ("Software Engineer", None, "unstated"),
    ("Software Engineer", 3.0, "no"),
    ("Software Engineer II", None, "no"),
    ("Software Engineer 2", 1.0, "no"),
    ("SDE2 - Payments", None, "no"),
    ("Senior Backend Engineer", None, "no"),
    ("Sr. Software Engineer", 1.0, "no"),
    ("Lead Engineer", None, "no"),
    ("Intermediate Backend Engineer, India", None, "no"),
    ("Backend Engineer Internship", None, "no"),
    ("2D & 3D Web Developer", 1.0, "fit"),  # "2D" is not a level
])
def test_classify(title, years, expected):
    assert levels.classify(title, years) == expected


def _add(db, title, location, category="java_backend", years_text=""):
    job, _ = job_svc.upsert(db, None, JobData(title=title, company_name="Examplecorp", location=location,
                                              description_text=f"Java, SQL (synthetic). {years_text}",
                                              source_url=f"https://example.com/{title.replace(' ', '-')}"), "manual")
    job.category = category
    job.relevance_score = 80.0
    db.commit()
    return job


def _titles(page):
    return {t for t in ("Fresh A", "Plain B", "SDE II C", "Senior D", "Far E", "Other F", "Dubai G") if t in page}


def test_jobs_page_defaults_to_fresher_roles_in_your_regions(client, db):
    fresh = _add(db, "Fresh A", "Pune, India", years_text="0-1 years of experience.")
    assert fresh.level_fit == "fit", "set at ingest"
    _add(db, "Plain B", "Remote")
    _add(db, "SDE II C", "Bengaluru, India")
    _add(db, "Senior D", "Remote")
    _add(db, "Far E", "Berlin, Germany")
    _add(db, "Other F", "Pune, India", category=None)
    _add(db, "Dubai G", "Dubai, UAE", years_text="5+ years of experience.")

    page = client.get("/jobs").text
    assert _titles(page) == {"Fresh A", "Plain B"}
    assert "Not shown: <b>3</b> above SDE-1" in page and "<b>1</b> outside your roles" in page
    assert "level not stated" in page and "Fresher / SDE-1" in page

    assert _titles(client.get("/jobs?level=fresher_stated").text) == {"Fresh A"}
    assert _titles(client.get("/jobs?level=any").text) == {"Fresh A", "Plain B", "SDE II C", "Senior D", "Dubai G"}
    assert _titles(client.get("/jobs?roles=any").text) == {"Fresh A", "Plain B", "Other F"}
    assert "Far E" not in client.get("/jobs?level=any&roles=any").text, "Elsewhere is never in the default view"
    assert "Far E" in client.get("/jobs?level=any&region=any").text

    # Tabs count what they show, and keep the other filters.
    tabs = client.get("/jobs?level=any").text
    assert 'Dubai / UAE <span class="n">1</span>' in tabs and 'All my regions <span class="n">5</span>' in tabs
    assert "level=any&amp;region=dubai" in tabs or "level=any&region=dubai" in tabs


def test_backfill_tags_existing_jobs(db):
    job = _add(db, "Senior D", "Remote")
    job.level_fit = None
    db.commit()
    assert levels.backfill(db) == 1 and job.level_fit == "no"
