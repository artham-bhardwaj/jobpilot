"""Milestone 21: robots.txt (RFC 9309), employer career-site adapters, parser fixes (synthetic data only)."""
from __future__ import annotations

import json

import pytest

from jobpilot.services.job_parser import classify_skills, parse_experience
from jobpilot.sources.robots import RobotsRules
from tests.helpers import make_http

UA = "JobPilot/0.1 (personal job-search assistant)"


def rules(text: str) -> RobotsRules:
    r = RobotsRules()
    r.parse(text.splitlines())
    return r


# ---- robots.txt ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,allowed", [
    ("/api/pcsx/search?q=x", True),    # longer Allow beats "Disallow: /" written first
    ("/careers/job/1", True),
    ("/admin", False),
    ("/", True),                       # "Allow: /$"
    ("/robots.txt", True),
])
def test_longest_match_wins(path, allowed):
    r = rules("User-agent: *\nDisallow: /\nAllow: /$\nAllow: /careers\nAllow: /api/pcsx\n")
    assert r.can_fetch(UA, f"https://jobs.example.com{path}") is allowed


def test_wildcards_ties_and_groups():
    r = rules("User-agent: Yandex\nDisallow: /jobs/results\n\nUser-agent: *\nDisallow: /jobs/results?page=\n"
              "Disallow: /*.pdf$\nAllow: /tie\nDisallow: /tie\n")
    assert r.can_fetch(UA, "https://e.example/jobs/results?q=java")       # Yandex's rule is not ours
    assert not r.can_fetch(UA, "https://e.example/jobs/results?page=2")
    assert not r.can_fetch(UA, "https://e.example/files/a.pdf") and r.can_fetch(UA, "https://e.example/files/a.pdf?x")
    assert r.can_fetch(UA, "https://e.example/tie")                        # equal length: allow wins


def test_named_group_replaces_star_and_empty_disallow():
    r = rules("User-agent: *\nDisallow: /\n\nUser-agent: jobpilot\nDisallow:\n")
    assert r.can_fetch(UA, "https://e.example/anything")
    assert not rules("User-agent: *\nDisallow: /\n").can_fetch(UA, "https://e.example/x")


def test_http_client_uses_rfc_rules(settings):
    settings.respect_robots_txt = True
    http = make_http(settings, {"https://jobs.example.com/robots.txt": (200, "User-agent: *\nDisallow: /\nAllow: /api\n"),
                                "https://jobs.example.com/api/x": (200, "{}"), "https://jobs.example.com/x": (200, "ok")})
    assert http.get("https://jobs.example.com/api/x").status_code == 200
    with pytest.raises(Exception, match="robots"):
        http.get("https://jobs.example.com/x")


# ---- adapters -----------------------------------------------------------------------------------------------------

EXC = ["senior", "principal", "manager"]


def _amazon_job(i: int, title: str) -> dict:
    return {"id": f"uuid-{i}", "id_icims": str(1000 + i), "title": title, "city": "Hyderabad", "country_code": "IND",
            "job_path": f"/en/jobs/{1000 + i}/x", "url_next_step": f"https://account.amazon.jobs/jobs/{1000 + i}/apply",
            "posted_date": "October 16, 2026", "job_schedule_type": "full-time", "job_category": "Software Development",
            "description": "Build payments services.<br/>Work with &amp; across teams.",
            "basic_qualifications": "- 1+ years of non-internship professional software development experience<br/>"
                                    "- Experience programming with Java",
            "preferred_qualifications": "- Bachelor's degree in computer science"}


def test_amazon_adapter_filters_titles_and_keeps_sections(settings):
    from jobpilot.sources.careers import AmazonJobsAdapter

    body = {"hits": 3, "jobs": [_amazon_job(1, "Software Development Engineer I"),
                                 _amazon_job(2, "Senior Software Development Engineer"), _amazon_job(3, "Recruiter")]}
    http = make_http(settings, {AmazonJobsAdapter.URL: (200, json.dumps(body))})
    res = AmazonJobsAdapter().fetch(http, {"queries": ["sde"], "country": "IND",
                                           "title_keywords": ["software development engineer"],
                                           "exclude_keywords": EXC}, None)
    (job,) = res.items
    assert (job.title, job.company_name, job.location, job.external_id) == \
           ("Software Development Engineer I", "Amazon", "Hyderabad, India", "1001")
    assert job.source_url == "https://www.amazon.jobs/en/jobs/1001/x" and job.posted_at.day == 16
    assert "Basic Qualifications" in job.description_text and "Preferred Qualifications" in job.description_text
    assert "<" not in job.description_text and "& across" in job.description_text
    assert not res.full_snapshot and res.counts == {"listed": 3, "matched": 1, "read": 1}
    assert parse_experience(job.description_text)[:2] == (1.0, None)


def test_amazon_config_validation():
    from jobpilot.sources.careers import AmazonJobsAdapter

    with pytest.raises(ValueError, match="title_keywords"):
        AmazonJobsAdapter().validate_config({"queries": ["x"]})
    with pytest.raises(ValueError, match="country"):
        AmazonJobsAdapter().validate_config({"queries": ["x"], "title_keywords": ["x"], "country": "India"})
    with pytest.raises(ValueError, match="queries"):
        AmazonJobsAdapter().validate_config({"title_keywords": ["x"]})


def test_microsoft_adapter_reads_details_for_kept_titles_only(settings):
    from jobpilot.sources.careers import MicrosoftCareersAdapter

    search = {"data": {"count": 2, "positions": [{"id": 11, "name": "Software Engineer II"},
                                                  {"id": 12, "name": "Principal Software Engineer"}]}}
    details = {"data": {"id": "11", "displayJobId": "200001", "name": "Software Engineer II",
                        "locations": ["India, Karnataka, Bangalore"], "postedTs": 1790248627,
                        "jobDescription": "<b>Qualifications</b><br><ul><li>2+ years of experience in Java</li></ul>",
                        "publicUrl": "https://apply.careers.microsoft.com/careers/job/11", "workLocationOption": "onsite",
                        "efcustomTextEmploymentType": ["Full-Time"], "department": "Software Engineering"}}
    base = MicrosoftCareersAdapter.BASE
    http = make_http(settings, {f"{base}/search": (200, json.dumps(search)),
                                f"{base}/position_details": (200, json.dumps(details))})
    res = MicrosoftCareersAdapter().fetch(http, {"queries": ["software engineer"], "location": "India",
                                                 "title_keywords": ["software engineer"], "exclude_keywords": EXC}, None)
    (job,) = res.items
    assert job.location == "Bangalore, Karnataka, India" and job.external_id == "200001" and job.remote_type == "onsite"
    assert "2+ years of experience in Java" in job.description_text and "<" not in job.description_text
    assert http.session.calls.count(f"{base}/position_details") == 1, "no detail request for excluded titles"
    with pytest.raises(ValueError, match="seniority"):
        MicrosoftCareersAdapter().validate_config({"queries": ["x"], "title_keywords": ["x"], "seniority": ["Junior"]})


def _google_page(jobs: list) -> str:
    data = json.dumps([jobs, None, len(jobs), 20])
    return f"<html><script>AF_initDataCallback({{key: 'ds:1', hash: '2', data:{data}, sideChannel: {{}}}});</script></html>"


def _google_job(jid: str, title: str) -> list:
    j = [None] * 21
    j[0], j[1], j[7] = jid, title, "Google"
    j[3] = [None, "<ul><li>Write code.</li></ul>"]
    j[4] = [None, "<h3>Minimum qualifications:</h3><ul><li>1 year of experience with software development.</li>"
                  "<li>Experience with Java.</li></ul><h3>Preferred qualifications:</h3><ul><li>Kubernetes.</li></ul>"]
    j[9] = [["Bengaluru, Karnataka, India", [], "Bengaluru", "560038", "KA", "IN"]]
    j[10] = [None, "<p>We're looking for engineers who bring fresh ideas from all areas, including large-scale "
                   "system design and natural language processing.</p>"]
    j[12] = [1788883642, 0]
    return j


def test_google_adapter_parses_embedded_results(settings):
    from jobpilot.services.matching import evaluate  # noqa: F401 - imported to make sure the module loads
    from jobpilot.sources.careers import GoogleCareersAdapter

    page = _google_page([_google_job("111", "Software Engineer II, Cloud"), _google_job("222", "Staff Software Engineer")])
    http = make_http(settings, {GoogleCareersAdapter.URL: (200, page)})
    res = GoogleCareersAdapter().fetch(http, {"queries": ["software engineer"], "location": "India",
                                              "target_level": ["EARLY"], "title_keywords": ["software engineer"],
                                              "exclude_keywords": ["staff"]}, None)
    (job,) = res.items
    assert job.source_url == f"{GoogleCareersAdapter.URL}/111-software-engineer-ii-cloud"
    assert job.location == "Bengaluru, Karnataka, India" and job.external_id == "111" and job.posted_at.year == 2026
    assert "Minimum qualifications:" in job.description_text
    ev, has_req = classify_skills(job.title, job.description_text, "Google")
    assert has_req and ev["java"]["kind"] == "required" and ev["kubernetes"]["kind"] == "preferred"
    assert ev["system design"]["kind"] == "mentioned", "company boilerplate is not a requirement"


@pytest.mark.parametrize("page", ["<html>no data</html>", _google_page([["only", "two"]])])
def test_google_adapter_fails_loudly_on_unknown_layout(settings, page):
    from jobpilot.sources.careers import GoogleCareersAdapter

    http = make_http(settings, {GoogleCareersAdapter.URL: (200, page)})
    with pytest.raises(Exception, match="nothing imported"):
        GoogleCareersAdapter().fetch(http, {"queries": ["x"], "title_keywords": ["x"]}, None)


def test_atlassian_adapter_filters_location_and_is_a_full_listing(settings):
    from jobpilot.sources.careers import AtlassianCareersAdapter

    def item(i, title, loc):
        return {"id": i, "title": title, "type": "Full-Time", "locations": [loc], "category": "Engineering",
                "overview": "<p>Working at Atlassian</p>", "responsibilities": "<ul><li>Build services</li></ul>",
                "qualifications": "<ul><li>4+ years of experience in Java</li></ul>",
                "applyUrl": f"https://globalcareers-atlassian.icims.com/jobs/{i}/x/job?mode=apply",
                "portalJobPost": {"portalUrl": f"https://globalcareers-atlassian.icims.com/jobs/{i}/x/job",
                                  "updatedDate": "2026-09-24 03:33 PM"}}

    body = [item(1, "Backend Software Engineer, II", "Bengaluru - India"), item(2, "Backend Software Engineer", "Sydney"),
            item(3, "Account Executive", "Bengaluru - India")]
    http = make_http(settings, {AtlassianCareersAdapter.URL: (200, json.dumps(body))})
    res = AtlassianCareersAdapter().fetch(http, {"location_keywords": ["india"], "title_keywords": ["software engineer"]},
                                          None)
    (job,) = res.items
    assert job.external_id == "1" and job.company_name == "Atlassian" and res.full_snapshot
    assert "Qualifications" in job.description_text and parse_experience(job.description_text)[:2] == (4.0, None)


def test_rerun_deduplicates_and_keeps_decisions(db, settings):
    """A second collection of the same postings updates nothing and creates no duplicate; decisions are kept."""
    from jobpilot.models import Job
    from jobpilot.services import jobs as job_svc
    from jobpilot.sources.careers import AmazonJobsAdapter

    body = json.dumps({"hits": 1, "jobs": [_amazon_job(1, "Software Development Engineer I")]})
    http = make_http(settings, {AmazonJobsAdapter.URL: (200, body)})
    src = job_svc.save_source(db, job_svc.SourceIn(name="Amazon", kind="amazon_jobs", company_name="Amazon",
                                                    config={"queries": ["sde"], "title_keywords": ["software development"]}))
    first = job_svc.run_source(db, http, src)
    (job,) = db.query(Job).all()
    job_svc.set_user_status(db, job, "shortlisted")
    second = job_svc.run_source(db, http, src)
    assert first["new"] == 1 and second["new"] == 0 and second["unchanged"] == 1
    assert db.query(Job).count() == 1 and db.get(Job, job.id).user_status == "shortlisted"


# ---- parser fixes found on real postings --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1+ years of non-internship professional software development experience", (1.0, None)),
    ("3+ years of non-internship professional software development experience", (3.0, None)),
    ("We value your experience. 2 years ago we launched a new product.", (None, None)),
    ("5 years in the industry. Our experience shows", (None, None)),
])
def test_long_qualifier_between_years_and_experience(text, expected):
    assert parse_experience(text)[:2] == expected


def test_requirement_wording_outside_a_requirements_section():
    text = ("We're looking for engineers with strong ideas from all areas, including system design and NLP.\n\n"
            "Minimum qualifications:\n• Experience with Java.\n\n"
            "Engineers must have hands-on Kafka experience.")
    ev, has_req = classify_skills("Software Engineer", text, None)
    assert has_req and ev["java"]["kind"] == "required"
    assert ev["system design"]["kind"] == "mentioned" and ev["nlp"]["kind"] == "mentioned"
    assert ev["kafka"]["kind"] == "required", "must-have wording still counts outside the section"
    # without a requirements section the wording is all we have, so it still counts
    ev2, has_req2 = classify_skills("Software Engineer", "We're looking for strong Java engineers.", None)
    assert not has_req2 and ev2["java"]["kind"] == "required"


# ---- add-url guard ------------------------------------------------------------------------------------------------

def test_greenhouse_board_index_is_not_imported_as_a_job(settings):
    from jobpilot.sources.pages import fetch_url

    with pytest.raises(ValueError, match="not a job posting"):
        fetch_url(make_http(settings, {}), "https://job-boards.greenhouse.io/appian?error=true")
    http = make_http(settings, {"https://www.example-careers.com/job/9": (
        302, "", {"Location": "https://job-boards.greenhouse.io/appian?error=true"}),
        "https://job-boards.greenhouse.io/appian?error=true": (200, "<html><h1>Current openings</h1></html>")})
    with pytest.raises(ValueError, match="no longer shows that posting"):
        fetch_url(http, "https://www.example-careers.com/job/9")
