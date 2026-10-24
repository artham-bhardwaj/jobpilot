"""Milestone 36: applying yourself from the dashboard — the job page's apply panel ("I applied" without leaving the
page), applied status and filter on the Jobs list, and "Ready to apply" on the Overview. Synthetic data only."""
from __future__ import annotations

from sqlalchemy import select

from jobpilot.models import Application, ResumeVersion
from tests.apply_fixtures import job, world  # noqa: F401


def _version(db):
    return db.scalar(select(ResumeVersion).where(ResumeVersion.status == "approved"))


def test_job_page_apply_panel_records_and_returns_to_the_job(world, client, db):  # noqa: F811
    j = job(db, "https://example.com/apply/1", title="Backend Engineer", company="Examplecorp")
    page = client.get(f"/jobs/{j.id}").text
    assert 'id="apply"' in page and f'action="/tracker/jobs/{j.id}/applied"' in page
    assert "https://example.com/apply/1" in page and "I applied" in page
    assert f'<option value="{_version(db).id}" selected>' in page, "the recommended resume is pre-selected"

    r = client.post(f"/tracker/jobs/{j.id}/applied", data={"resume_version_id": str(_version(db).id),
                                                           "next": f"/jobs/{j.id}"})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{j.id}")
    page = client.get(f"/jobs/{j.id}").text
    assert "✓ Applied" in page and "I applied</button>" not in page


def test_tracker_return_path_stays_local(world, client, db):  # noqa: F811
    j = job(db, "https://example.com/apply/2")
    r = client.post(f"/tracker/jobs/{j.id}/later", data={"next": "//evil.example/jobs"})
    assert r.headers["location"].startswith("/tracker")
    r = client.post(f"/tracker/jobs/{j.id}/later", data={"next": "https://evil.example/"})
    assert r.headers["location"].startswith("/tracker")


def test_tailored_option_without_a_tailored_resume_is_refused(world, client, db):  # noqa: F811
    j = job(db, "https://example.com/apply/3")
    r = client.post(f"/tracker/jobs/{j.id}/applied", data={"resume_version_id": "tailored", "next": f"/jobs/{j.id}"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert db.scalar(select(Application).where(Application.job_id == j.id)) is None


def test_jobs_list_applied_filter_badge_and_overview_ready_list(world, client, db):  # noqa: F811
    done = job(db, "https://example.com/apply/4", title="Applied Role", company="Acme")
    todo = job(db, "https://example.com/apply/5", title="Open Role", company="Beta")
    client.post(f"/tracker/jobs/{done.id}/applied", data={"resume_version_id": str(_version(db).id)})

    page = client.get("/jobs").text
    assert "✓ applied" in page and 'class="row-applied"' in page
    only_open = client.get("/jobs?applied=no").text
    assert "Open Role" in only_open and "Applied Role" not in only_open
    only_done = client.get("/jobs?applied=yes").text
    assert "Applied Role" in only_done and "Open Role" not in only_done

    home = client.get("/").text
    assert "Ready to apply" in home and f'href="/jobs/{todo.id}#apply"' in home
    assert f'href="/jobs/{done.id}#apply"' not in home
