"""SDE / backend role filter, its use in the cleanup, the Y Combinator jobs source and the new source presets."""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone

import pytest

from jobpilot.services import cleanup, regional_sources, roles, settings_store
from jobpilot.services import jobs as job_svc
from jobpilot.sources import ADAPTERS
from jobpilot.sources.base import JobData
from jobpilot.sources.yc import YCJobsAdapter, location_text

KEEP = ["Software Development Engineer I", "SDE-1 (FTC)", "Software Engineer, New Grad", "Python Backend Developer",
        "Backend Developer - Go", "Java Fullstack Developer | Offshore", ".NET Developer", "Software Engineer : AI",
        "Software Dev Engineer, Alexa AI", "Software Engineer I - Java Full Stack React/Angular",
        "Full Stack Developer (React + Node.js)", "Remote C/C++ Software Engineer - Data Recovery Tools",
        "Software Engineer - Cloud: Azure", "Software Engineer, Growth", "Member of Technical Staff",
        "Web Developer - JavaScript/Node JS/React", "Founding Software Engineer – Backend & Cloud Infrastructure"]
DROP = ["Android Developer", "React Native Developer", "React JS Developer", "Software Engineer - L3 Support",
        "Software Engineer - Site Reliability Engineer", "SDE III - Devops", "Associate Data Engineer",
        "Software Development Engineer in Test II", "QA Automation Engineer", "Data Scientist", "AI Agent Developer",
        "Software Engineer, AI Coding Tasks (Contract)", "Backend Developer - G2i Talent Pool",
        "Software Engineer (Open Source Contributor)", "Firmware Engineer", "Salesforce Developer", "n8n Developer",
        "Software Engineer - Solutions Engineering", "Head of Sales/GTM", "Product Manager", "Videographer",
        "Web Developer (Fresh graduate welcome)", "Go-To-Market Lead", "UI/UX Designer", ""]


@pytest.mark.parametrize("title", KEEP)
def test_sde_and_backend_titles_are_kept(title):
    assert roles.check(title) == (True, None)


@pytest.mark.parametrize("title", DROP)
def test_other_roles_are_left_out_with_a_reason(title):
    ok, why = roles.check(title)
    assert not ok and why


def _add(db, title):
    job, _ = job_svc.upsert(db, None, JobData(title=title, company_name="Examplecorp", location="Pune, India",
                                              description_text="Java, SQL (synthetic).",
                                              source_url=f"https://example.com/{title.replace(' ', '-')}"), "manual")
    job.category, job.relevance_score = "general_swe", 80.0
    db.commit()
    return job


def test_cleanup_hides_non_sde_roles_unless_switched_off(db):
    keep = _add(db, "Software Engineer")
    android = _add(db, "Android Developer")
    p = cleanup.plan(db)
    assert [(j.id, r) for j, r in p["hide"]] == [(android.id, ["role"])]
    assert cleanup.label("cleanup:role") == "not an SDE / backend role"
    assert keep not in [j for j, _ in p["hide"]]

    settings_store.put(db, "job_cleanup", {**settings_store.get(db, "job_cleanup"), "sde_backend_only": False})
    db.commit()
    assert cleanup.plan(db)["hide"] == [], "with the switch off only the category decides"


def test_cleanup_setting_defaults_on_for_settings_saved_before():
    settings_store.validate("job_cleanup", {"auto": True, "min_score": 40})
    assert settings_store.DEFAULTS["job_cleanup"]["sde_backend_only"] is True


# ---- Y Combinator ------------------------------------------------------------------------------------------------

def _page(props: dict) -> str:
    return f'<html><body><div id="app" data-page="{html.escape(json.dumps({"props": props}))}"></div></body></html>'


def _posting(title: str, posted: datetime) -> str:
    ld = {"@context": "https://schema.org/", "@type": "JobPosting", "title": title,
          "description": "<p>Build APIs in Java (synthetic).</p>", "datePosted": posted.strftime("%Y-%m-%dT%H:%M:%SZ"),
          "hiringOrganization": {"@type": "Organization", "name": "Acme"},
          "jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": "IN"}}]}
    return f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, accept="*/*", params=None):
        from jobpilot.sources.http import FetchError

        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(f"HTTP 404 for {url}", status=404)
        return FakeResponse(self.routes[url])


def test_yc_location_codes_are_written_out():
    assert location_text("IN / Remote (IN)") == "India / Remote (India)"
    assert location_text("Bengaluru, KA, IN") == "Bengaluru, KA, India"
    assert location_text("San Francisco, CA, US / Remote (US; CA)") == \
        "San Francisco, CA, United States / Remote (United States; Canada)"


def test_yc_source_keeps_wanted_recent_postings_in_your_regions():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = "https://www.ycombinator.com"
    listing = [
        {"id": 1, "title": "Software Engineer, Full-Stack", "url": "/companies/acme/jobs/a1-swe", "companyName": "Acme",
         "location": "IN / Remote (IN)", "minExperience": "Any (new grads ok)", "type": "Full-time"},
        {"id": 2, "title": "Backend Engineer", "url": "/companies/beta/jobs/b2-be", "companyName": "Beta",
         "location": "Remote (US)"},
        {"id": 3, "title": "Senior Software Engineer", "url": "/companies/acme/jobs/c3-sse", "companyName": "Acme",
         "location": "IN"},
        {"id": 4, "title": "Software Engineer", "url": "/companies/old/jobs/d4-old", "companyName": "Old",
         "location": "South East Asia & India"},
        {"id": 5, "title": "Product Manager", "url": "/companies/acme/jobs/e5-pm", "companyName": "Acme",
         "location": "IN"},
    ]
    http = FakeHttp({
        f"{base}/jobs/role/software-engineer/india": _page({"jobPostings": listing}),
        f"{base}/jobs/role/software-engineer/remote": _page({"jobPostings": listing[:2]}),
        f"{base}/companies/acme/jobs/a1-swe": _posting("Software Engineer, Full-Stack", now - timedelta(days=5)),
        f"{base}/companies/old/jobs/d4-old": _posting("Software Engineer", now - timedelta(days=1500)),
    })
    cfg = {"locations": ["india", "remote"], "title_keywords": ["software engineer", "backend"],
           "exclude_keywords": ["senior"], "regions": ["india", "remote", "dubai"]}
    res = YCJobsAdapter().fetch(http, cfg, None)
    assert res.errors == []
    assert [j.title for j in res.items] == ["Software Engineer, Full-Stack"]
    job = res.items[0]
    assert job.company_name == "Acme" and job.location == "India / Remote (India)" and job.remote_type == "remote"
    assert job.apply_url == f"{base}/companies/acme/jobs/a1-swe" and job.external_id == "1"
    assert "Experience: Any (new grads ok)" in job.description_text
    assert f"{base}/companies/beta/jobs/b2-be" not in http.calls, "US-only remote posting is never opened"
    assert any("older than 180 days" in n for n in res.notes)


def test_yc_source_fails_loudly_when_the_page_changes():
    from jobpilot.sources.http import FetchError

    http = FakeHttp({"https://www.ycombinator.com/jobs/role/software-engineer/india": "<html>new layout</html>"})
    with pytest.raises(FetchError):
        YCJobsAdapter().fetch(http, {"locations": ["india"], "title_keywords": ["software engineer"]}, None)
    with pytest.raises(ValueError):
        YCJobsAdapter().validate_config({"locations": ["mars"], "title_keywords": ["x"]})


# ---- presets -----------------------------------------------------------------------------------------------------

def test_every_preset_is_a_valid_source_with_a_unique_name():
    names = [p["name"] for p in regional_sources.PRESETS]
    assert len(names) == len(set(names))
    for p in regional_sources.PRESETS:
        ADAPTERS[p["kind"]].validate_config(p["config"])
    kinds = {p["kind"] for p in regional_sources.PRESETS}
    assert {"yc_jobs", "workday", "lever", "greenhouse"} <= kinds
    assert "Citi (Workday)" in names and "Meesho (Lever)" in names and "Pure Storage (Greenhouse)" in names
    # Fintech and bank / exchange tech centres.
    assert {"Stripe (Greenhouse)", "Morgan Stanley (Workday)", "Deutsche Bank (Workday)", "Nasdaq (Workday)"} <= set(names)


def test_regional_sources_adds_the_new_presets_once(db):
    first = regional_sources.apply(db)
    db.commit()
    assert "Y Combinator startups (India + remote)" in first["add"] and "Citi (Workday)" in first["add"]
    assert regional_sources.plan(db)["add"] == []
