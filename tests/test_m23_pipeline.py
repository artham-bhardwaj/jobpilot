"""Milestone 23: job-pipeline quality defects found on the live job set (synthetic data only).

- Workday JobPosting descriptions arrive as one flattened line, so the whole posting looked "preferred".
- SDE I (a level) was assigned from description skills alone (a "Senior Computer Scientist - I (C++)").
- Amazon's "Software Dev Engineer" / "SDE" titles got no family.
- An unstated work mode cost location points even though every work mode suits the candidate.
- Workday search adapter (JSON POST) and the HTTP client's POST.
"""
from __future__ import annotations

import json

import pytest

from jobpilot.services.matching import classify
from jobpilot.services.settings_store import DEFAULTS
from jobpilot.sources.base import JobData
from tests.helpers import make_http

CATS = DEFAULTS["role_categories"]
MC = "https://mastercard.wd1.myworkdayjobs.com"
API = f"{MC}/wday/cxs/mastercard/CorporateCareers"
FLAT = ("Our Purpose Mastercard powers economies. Overview We are seeking an engineer. Required Qualifications • 4+ "
        "years of software engineering experience. • Strong experience with Java. Preferred Qualifications • Kafka.")
HTML = ("<p><b>Overview</b></p><p>We are seeking an engineer.</p><p><b>Required Qualifications</b></p><ul>"
        "<li>4+ years of software engineering experience.</li><li>Strong experience with Java.</li></ul>"
        "<p><b>Preferred Qualifications</b></p><ul><li>Kafka.</li></ul>")


def _info(path="/job/Pune-India/Software-Engineer-II_R-1", title="Software Engineer II", end="2026-11-21", **kw):
    return {"jobPostingInfo": {"id": "abc", "title": title, "jobDescription": HTML, "location": "Pune, India",
                               "country": {"descriptor": "India"}, "startDate": "2026-10-16", "endDate": end,
                               "timeType": "Full time", "jobReqId": "R-1", "externalUrl": f"{MC}/CorporateCareers{path}",
                               **kw}}


# ---- Workday descriptions ----------------------------------------------------------------------------------

def test_flattened_workday_description_is_replaced_by_the_html_one(settings):
    from jobpilot.services.job_parser import classify_skills, split_sections
    from jobpilot.sources.pages import enrich_workday

    url = f"{MC}/CorporateCareers/job/Pune-India/Software-Engineer-II_R-1"
    req, pref, _ = split_sections(FLAT)
    assert req == "" and "4+ years" in pref, "the defect: one line, so everything counts as preferred"
    http = make_http(settings, {f"{API}/job/Pune-India/Software-Engineer-II_R-1": (200, json.dumps(_info()))})
    job = enrich_workday(http, JobData(title="Software Engineer II", source_url=url, description_text=FLAT,
                                       description_raw=FLAT))
    assert job.description_raw == HTML and job.raw["description_source"] == "workday job API (HTML)"
    assert job.raw["jsonld_description_chars"] == len(FLAT)
    req, pref, _ = split_sections(job.description_text)
    assert "4+ years" in req and "Kafka" in pref
    ev, _ = classify_skills(job.title, job.description_text, "Mastercard")
    assert ev["java"]["kind"] == "required" and ev["kafka"]["kind"] == "preferred"


@pytest.mark.parametrize("route", [(403, '{"errorCode":"S22"}'), (200, json.dumps({"jobPostingInfo": {"jobDescription": "x"}}))])
def test_workday_enrichment_keeps_the_jsonld_text_on_failure(settings, route):
    from jobpilot.sources.pages import enrich_workday

    url = f"{MC}/CorporateCareers/job/Pune-India/Software-Engineer-II_R-1"
    http = make_http(settings, {f"{API}/job/Pune-India/Software-Engineer-II_R-1": route})
    job = enrich_workday(http, JobData(title="X", source_url=url, description_text=FLAT))
    assert job.description_text == FLAT and not (job.raw or {}).get("description_source")


def test_enrichment_only_touches_flattened_workday_postings(settings):
    from jobpilot.sources.pages import enrich_workday

    http = make_http(settings, {})
    structured = JobData(title="X", source_url=f"{MC}/CorporateCareers/job/a_R-1", description_text="a\nb")
    other = JobData(title="X", source_url="https://careers.example.com/job/1", description_text=FLAT)
    assert enrich_workday(http, structured).description_text == "a\nb"
    assert enrich_workday(http, other).description_text == FLAT and http.session.calls == []


# ---- Workday search adapter --------------------------------------------------------------------------------

def test_workday_search_filters_reads_details_and_dedupes(db, settings):
    from jobpilot.models import Job
    from jobpilot.services import jobs as job_svc
    from jobpilot.sources.workday import WorkdayAdapter

    search = {"total": 3, "jobPostings": [
        {"title": "Software Engineer II", "externalPath": "/job/Pune-India/Software-Engineer-II_R-1", "locationsText": "Pune, India"},
        {"title": "Engineering Manager", "externalPath": "/job/Pune-India/Engineering-Manager_R-2", "locationsText": "Pune, India"},
        {"title": "Software Engineer II", "externalPath": "/job/Dublin/Software-Engineer-II_R-3", "locationsText": "Dublin, Ireland"}]}
    http = make_http(settings, {f"{API}/jobs": (200, json.dumps(search)),
                                f"{API}/job/Pune-India/Software-Engineer-II_R-1": (200, json.dumps(_info()))})
    cfg = {"site_url": f"{MC}/CorporateCareers", "queries": ["software engineer india"],
           "title_keywords": ["software engineer"], "location_keywords": ["india"], "exclude_keywords": ["manager"]}
    res = WorkdayAdapter().fetch(http, cfg, "Mastercard")
    (j,) = res.items
    assert http.session.posts[0] == (f"{API}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0,
                                                    "searchText": "software engineer india"})
    assert (j.external_id, j.location, j.valid_through.date().isoformat()) == ("R-1", "Pune, India", "2026-11-21")
    assert j.raw["deadline_source"] == "Workday endDate" and "4+ years" in j.description_text
    assert not res.full_snapshot and res.counts == {"listed": 3, "matched": 1, "read": 1}
    # an existing sitemap-collected job with the same public URL is updated, not duplicated
    src = job_svc.save_source(db, job_svc.SourceIn(name="Mastercard (Workday)", kind="workday", company_name="Mastercard",
                                                    config=cfg))
    old = JobData(title="Software Engineer II", company_name="Mastercard", location="Pune, India",
                  description_text=FLAT, source_url=j.source_url, apply_url=j.source_url, external_id="R-1")
    existing, _ = job_svc.upsert(db, src, old, "sitemap")
    job_svc.set_user_status(db, existing, "shortlisted")
    stats = job_svc.run_source(db, http, src)
    assert stats["updated"] == 1 and stats["new"] == 0 and db.query(Job).count() == 1
    refreshed = db.get(Job, existing.id)
    assert refreshed.user_status == "shortlisted" and "\n" in refreshed.description_text


def test_workday_config_and_posting_path_validation(settings):
    from jobpilot.sources.workday import WorkdayAdapter

    with pytest.raises(ValueError, match="Workday career site"):
        WorkdayAdapter().validate_config({"site_url": "https://example.com/jobs", "queries": ["x"], "title_keywords": ["x"]})
    search = {"total": 1, "jobPostings": [{"title": "Software Engineer", "externalPath": "/../../admin"}]}
    http = make_http(settings, {f"{API}/jobs": (200, json.dumps(search))})
    res = WorkdayAdapter().fetch(http, {"site_url": f"{MC}/CorporateCareers", "queries": ["x"],
                                        "title_keywords": ["software engineer"]}, None)
    assert res.items == [] and "unexpected posting path" in res.errors[0]


def test_post_json_obeys_robots(settings):
    settings.respect_robots_txt = True
    http = make_http(settings, {f"{MC}/robots.txt": (200, "User-agent: *\nDisallow: /wday/\n")})
    with pytest.raises(Exception, match="robots"):
        http.post_json(f"{API}/jobs", {"searchText": "x"})
    assert http.session.posts == []


# ---- classification ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("title,desc,expected", [
    ("Senior Computer Scientist - I (C++)", "c++ oop data structures algorithms", None),  # not sde1 from skills alone
    ("Computer Scientist - II", "java python c++ oop", None),
    ("Software Development Engineer I", "java", "sde1"),
    ("SDE-1 (FTC)", "", "sde1"),
    ("Software Dev Engineer, Alexa Connections", "", "general_swe"),
    ("SDE, Alexa For Shopping", "", "general_swe"),
    ("Software Engineer II", "java", "general_swe"),
])
def test_role_family(title, desc, expected):
    got = classify(title, desc, CATS)["id"]
    if expected is None:
        assert got != "sde1"
    else:
        assert got == expected


# ---- location scoring with an unstated work mode -----------------------------------------------------------

def _location_points(db, remote_type, prefs_modes, pref_locs=()):
    from jobpilot.models import Job, utcnow
    from jobpilot.services import matching, settings_store

    settings_store.put(db, "search_preferences", {**DEFAULTS["search_preferences"], "remote_preferences": prefs_modes,
                                                  "locations": list(pref_locs)})
    job = Job(title="Software Engineer", company_name="Examplecorp", location="Bengaluru, India", status="active",
              user_status="new", fingerprint=f"fp-{remote_type}-{len(prefs_modes)}-{len(pref_locs)}",
              remote_type=remote_type, description_text="Requirements:\n- Java", first_seen_at=utcnow(),
              last_seen_at=utcnow())
    db.add(job)
    db.flush()
    matching.match_job(db, job)
    comp = next(c for c in job.match_details["relevance"]["components"] if c["key"] == "location")
    return comp["points"], comp["explanation"], job.match_details


@pytest.fixture
def profile(db):
    from tests.helpers import seed_profile

    seed_profile(db)
    return db


def test_unstated_work_mode_costs_nothing_when_every_mode_suits_you(db, profile):
    onsite, _, _ = _location_points(db, "onsite", ["remote", "hybrid", "onsite"])
    unknown, why, details = _location_points(db, "unknown", ["remote", "hybrid", "onsite"])
    assert unknown == onsite == 12.0 and "any work mode suits" in why
    assert "work arrangement (remote/hybrid/onsite)" in details["missing_job_info"], "still reported as missing"


def test_unstated_work_mode_still_partial_when_you_exclude_a_mode(db, profile):
    points, why, _ = _location_points(db, "unknown", ["remote", "hybrid"])
    assert points == 6.0 and why == "work mode not stated (partial credit)"


def test_unstated_work_mode_uses_preferred_locations(db, profile):
    hit, _, _ = _location_points(db, "unknown", ["remote", "hybrid", "onsite"], ["Bengaluru"])
    miss, why, _ = _location_points(db, "unknown", ["remote", "hybrid", "onsite"], ["Pune"])
    assert hit == 12.0 and miss == pytest.approx(2.4) and "not one of your preferred locations" in why


# ---- parser: degree and "you will learn" -------------------------------------------------------------------

@pytest.mark.parametrize("text,degree", [
    ("If interested in a front end role, eagerness to master a front end technology stack.", None),  # the verb
    ("You will master the art of Java services.", None),
    ("Bachelor's or Master's degree in Computer Science", "Bachelor's or Master"),
    ("Master of Science in Computer Science", "Master of Science in Computer Science"),
    ("Masters in Computer Science", "Masters in Computer Science"),
    ("Master’s degree required", "Master’s degree required"),
])
def test_master_the_verb_is_not_a_degree(text, degree):
    from jobpilot.services import job_parser

    got = job_parser.parse("Software Engineer", f"Requirements:\n- {text}", None).degree_required
    assert (got is None) == (degree is None)
    if degree:
        assert got.startswith(degree.split()[0])


@pytest.mark.parametrize("sentence,skill,kind", [
    ("Learn and use math, statistics, and design to solve important problems.", "statistics", "preferred"),
    ("You will learn Kubernetes from senior engineers.", "kubernetes", "preferred"),
    ("Build machine learning pipelines.", "machine learning", "required"),  # "learning" inside a skill name
    ("Strong experience with Java is required.", "java", "required"),
])
def test_skills_you_will_learn_are_not_requirements(sentence, skill, kind):
    from jobpilot.services.job_parser import classify_skills

    ev, _ = classify_skills("Software Engineer", sentence, None)
    assert ev[skill]["kind"] == kind and ev[skill]["snippet"].startswith(sentence[:20]), "evidence is the sentence"
