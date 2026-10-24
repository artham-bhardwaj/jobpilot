"""Milestone 15: sitemap source (career-site sitemap + JobPosting pages), offline. Synthetic data only."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from jobpilot.models import Job, JobSource
from jobpilot.services import jobs as job_svc
from jobpilot.sources import ADAPTERS
from tests.helpers import make_http, seed_profile

HOST = "https://example.wd5.myworkdayjobs.com"
SITEMAP = f"{HOST}/Careers/siteMap.xml"


def posting(title, city="Bengaluru", country="India", ref="R1"):
    data = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "datePosted": "2026-10-15",
            "employmentType": "FULL_TIME", "identifier": {"@type": "PropertyValue", "value": ref},
            "hiringOrganization": {"@type": "Organization", "name": "Examplecorp"},
            "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": f"{city}, {country}",
                                                          "addressCountry": country}},
            "description": "<p>Build Java and Spring Boot services. REST APIs, SQL. 0-2 years of experience.</p>"}
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body>x</body></html>'


URLS = {
    "java": f"{HOST}/Careers/job/IN---Bengaluru-India/Java-Backend-Engineer_R1",
    "sde": f"{HOST}/Careers/job/IN---Pune-India/Software-Engineer-I_R2",
    "director": f"{HOST}/Careers/job/IN---Bengaluru-India/Director--Software-Engineering_R3",
    "sales": f"{HOST}/Careers/job/IN---Bengaluru-India/Sales-Manager_R4",
    "us": f"{HOST}/Careers/job/US---Austin-TX/Software-Engineer_R5",
    "gone": f"{HOST}/Careers/job/IN---Pune-India/Python-Backend-Engineer_R6",
    "offsite": "https://other.example.org/Careers/job/IN/Software-Engineer_R7",
}
SITEMAP_XML = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
               "".join(f"<url><loc>{u}</loc><lastmod>2026-10-16</lastmod></url>" for u in URLS.values()) + "</urlset>")
CONFIG = {"sitemap_url": SITEMAP, "title_keywords": ["software engineer", "backend", "sde"],
          "location_keywords": ["india"], "exclude_keywords": ["director"], "max_pages": 10}


def routes(**extra):
    r = {f"{HOST}/robots.txt": (200, "User-agent: *\nAllow: /Careers/\nDisallow: /refreshFacet/\n"),
         SITEMAP: (200, SITEMAP_XML, {"Content-Type": "application/xml"}),
         URLS["java"]: (200, posting("Java Backend Engineer"), {"Content-Type": "text/html"}),
         URLS["sde"]: (200, posting("Software Engineer I", city="Pune", ref="R2"), {"Content-Type": "text/html"}),
         URLS["gone"]: (404, "not found")}
    r.update(extra)
    return r


def test_config_validation():
    a = ADAPTERS["sitemap"]
    assert a.validate_config(CONFIG)["title_keywords"] == ["software engineer", "backend", "sde"]
    for bad in ({"sitemap_url": "http://x.example.com/s.xml", "title_keywords": ["a"]},
                {"sitemap_url": "https://x.example.com/page.html", "title_keywords": ["a"]},
                {"sitemap_url": SITEMAP, "title_keywords": []},
                {"sitemap_url": SITEMAP, "title_keywords": ["a"], "max_pages": 500}):
        with pytest.raises(ValueError):
            a.validate_config(bad)


def test_selects_only_matching_same_host_postings():
    a = ADAPTERS["sitemap"]
    urls, matched = a.select(a.validate_config(CONFIG), list(URLS.values()))
    assert urls == [URLS["java"], URLS["sde"], URLS["gone"]] and matched == 3
    urls, matched = a.select(a.validate_config({**CONFIG, "max_pages": 1}), list(URLS.values()))
    assert urls == [URLS["java"]] and matched == 3


def test_fetch_reads_jobposting_pages_and_respects_robots(settings):
    http = make_http(settings, routes())
    res = ADAPTERS["sitemap"].fetch(http, CONFIG, "Examplecorp")
    assert [j.title for j in res.items] == ["Java Backend Engineer", "Software Engineer I"]
    assert res.items[0].location == "Bengaluru, India" and res.items[0].company_name == "Examplecorp"
    assert res.gone_urls == [URLS["gone"]] and not res.full_snapshot
    assert res.counts == {"sitemap_urls": 7, "matched": 3, "read": 3}
    assert URLS["director"] not in http.session.calls and URLS["offsite"] not in http.session.calls
    settings.respect_robots_txt = True  # the test fixture turns it off; JobPilot's default is on
    blocked = make_http(settings, routes(**{f"{HOST}/robots.txt": (200, "User-agent: *\nDisallow: /\n")}))
    with pytest.raises(Exception, match="robots"):
        ADAPTERS["sitemap"].fetch(blocked, CONFIG, "Examplecorp")


def test_collect_saves_scores_and_never_duplicates(db, settings):
    seed_profile(db)
    src = job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp (Workday)", kind="sitemap", company_name="Examplecorp",
                                                   config=CONFIG))
    dry = job_svc.collect_all(db, settings, make_http(settings, routes()), dry_run=True)
    assert dry["new"] == 2 and db.scalar(select(func.count(Job.id))) == 0, "a dry run saves nothing"
    first = job_svc.collect_all(db, settings, make_http(settings, routes()))
    assert first["new"] == 2
    jobs = list(db.scalars(select(Job).order_by(Job.id)))
    assert [(j.title, j.company_name, j.source_url) for j in jobs] == [
        ("Java Backend Engineer", "Examplecorp", URLS["java"]), ("Software Engineer I", "Examplecorp", URLS["sde"])]
    assert all(j.relevance_score is not None and j.match_details for j in jobs), "scored and explained"
    second = job_svc.collect_all(db, settings, make_http(settings, routes()))
    assert second["new"] == 0 and db.scalar(select(func.count(Job.id))) == 2
    assert db.get(JobSource, src.id).last_status == "partial", "one posting page answered 404: reported, not hidden"
    from jobpilot.models import SourceRun

    run = db.scalar(select(SourceRun).where(SourceRun.dry_run.is_(False)).order_by(SourceRun.id.desc()))
    assert run.warnings == ["sitemap lists 7 posting(s); 3 matched your keywords; read 3"] and run.new == 0
    clean = {**CONFIG, "title_keywords": ["software engineer", "java backend"]}
    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp (Workday)", kind="sitemap", company_name="Examplecorp",
                                             config=clean), src.id)
    assert job_svc.collect_all(db, settings, make_http(settings, routes()))["new"] == 0
    assert db.get(JobSource, src.id).last_status == "success" and db.scalar(select(func.count(Job.id))) == 2


def test_same_requisition_from_another_site_is_a_duplicate_not_a_new_job(db, settings):
    seed_profile(db)
    manual, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Software Engineer I", company_name="Examplecorp",
                                                           location="Pune, Maharashtra, India", description_text="Java"))
    manual.external_id = "R2"
    db.flush()
    job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp (Workday)", kind="sitemap", company_name="Examplecorp",
                                             config=CONFIG))
    res = job_svc.collect_all(db, settings, make_http(settings, routes()))
    assert res["new"] == 1, "only the Java role is new; the SDE I posting is the job already recorded"
    assert db.scalar(select(func.count(Job.id))) == 2
    # The same requisition id at a different company is a different job.
    from jobpilot.sources.base import JobData

    other = JobData(title="Platform Engineer", company_name="Othercorp", external_id="R2", location="Pune, India")
    assert job_svc.find_existing(db, None, other) == (None, None)


def test_experience_alternatives_for_phd_holders_are_not_the_requirement():
    from jobpilot.services.job_parser import parse_experience

    visa = ("7+ years of relevant work experience with a Bachelor’s Degree or at least 2 years of work experience with "
            "an Advanced degree (e.g. Masters, MBA, JD, MD) or 0 years of work experience with a PhD")
    assert parse_experience(visa)[0] == 7.0
    assert parse_experience("0-2 years of experience in Java")[:2] == (0.0, 2.0)
    assert parse_experience("3 years of experience with a PhD")[0] == 3.0, "if that's all there is, it is used"


def test_pages_without_charset_are_decoded_as_utf8(settings):
    page = posting("Software Engineer I").replace("Build Java", "Bachelor’s degree. Build Java")
    http = make_http(settings, {URLS["sde"]: (200, page.encode("utf-8"), {"Content-Type": "text/html"})})
    (job,) = ADAPTERS["jsonld"].fetch(http, {"urls": [URLS["sde"]]}, "Examplecorp").items
    assert "Bachelor’s" in job.description_text and "â€" not in job.description_text
