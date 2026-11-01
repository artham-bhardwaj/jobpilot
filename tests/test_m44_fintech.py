"""Milestone 44: the Fintech jobs page — jobs at fintech, bank, exchange and trading companies (synthetic data only)."""
from __future__ import annotations

import pytest

from jobpilot.services import jobs as job_svc
from jobpilot.services import sectors
from jobpilot.sources.base import JobData


def _add(db, title, company, location="Pune, India"):
    job, _ = job_svc.upsert(db, None, JobData(title=title, company_name=company, location=location,
                                              description_text="Java, Spring Boot, SQL (synthetic)",
                                              source_url=f"https://example.com/{title.replace(' ', '-')}"), "manual")
    job.relevance_score = 80.0
    job.category = "java_backend"
    db.commit()
    return job


@pytest.mark.parametrize("company, expected", [
    ("JPMorgan Chase & Co.", True), ("Deutsche Bank AG", True), ("Morgan Stanley", True), ("Zerodha", True),
    ("PhonePe Private Limited", True), ("LSEG (London Stock Exchange Group)", True), ("Coinbase", True),
    ("Citizens Advice", False), ("Credence Systems", False), ("Examplecorp", False), (None, False), ("", False),
])
def test_fintech_companies_match_on_whole_words(db, company, expected):
    assert sectors.is_fintech(company) is expected
    if company:  # the SQL filter agrees with the Python check
        job = _add(db, f"Backend Engineer {company}", company)
        assert (job.id in {j.id for j in job_svc.search(db, sector="fintech")[0]}) is expected


def test_fintech_page_lists_only_fintech_jobs_and_keeps_its_filters(client, db):
    bank = _add(db, "Java Developer Bank", "Morgan Stanley")
    exchange = _add(db, "Backend Engineer Exchange", "Nasdaq", location="Dubai, United Arab Emirates")
    other = _add(db, "Backend Engineer Retail", "Examplecorp")

    page = client.get("/fintech").text
    assert "<h1>Fintech jobs</h1>" in page
    assert f'href="/jobs/{bank.id}' in page and f'href="/jobs/{exchange.id}' in page
    assert f'href="/jobs/{other.id}' not in page
    assert '<a href="/fintech" class="active" aria-current="page">Fintech jobs</a>' in page, "its own menu entry"
    assert 'action="/fintech"' in page and 'href="/fintech?region=dubai"' in page, "filters stay on the page"
    assert "/jobs/export.pdf?sector=fintech" in page

    dubai = client.get("/fintech?region=dubai").text
    assert f'href="/jobs/{exchange.id}' in dubai and f'href="/jobs/{bank.id}' not in dubai
    assert f'href="/jobs/{other.id}' in client.get("/jobs").text, "the Jobs page is unchanged"

    export = client.get("/jobs/export.xlsx?sector=fintech")
    assert export.status_code == 200 and "fintech" in export.headers["content-disposition"]
