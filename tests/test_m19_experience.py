"""Milestone 19: experience requirements in months and in reversed wording (synthetic postings)."""
from __future__ import annotations

import pytest

from jobpilot.services.job_parser import parse_experience


@pytest.mark.parametrize("text,expected", [
    # months (converted to years, the stored unit)
    ("0 to 12 months of experience in the software engineering field", (0.0, 1.0)),
    ("6 months of experience", (0.5, None)),
    ("6-18 months of relevant experience", (0.5, 1.5)),
    # reversed wording, with hyphen, en dash and em dash
    ("Overall career experience of 8–12 years into Software Engineering", (8.0, 12.0)),
    ("experience of 8-12 years", (8.0, 12.0)),
    ("experience of 8—12 years", (8.0, 12.0)),
    ("Experience: 3+ years in Java", (3.0, None)),
    ("experience of at least 2 years", (2.0, None)),
    # existing formats (regression)
    ("8+ years of experience", (8.0, None)),
    ("3–6 years of experience", (3.0, 6.0)),
    ("3 — 6 years of experience", (3.0, 6.0)),
    ("3 to 5 years of relevant experience in Java", (3.0, 5.0)),
    ("minimum of 4 years in backend development", (4.0, None)),
    ("0 years of experience", (0.0, None)),
    ("7+ years of relevant work experience with a Bachelor's Degree or 0 years of work experience with a PhD", (7.0, None)),
])
def test_stated_requirements(text, expected):
    lo, hi, snippet = parse_experience(text)
    assert (lo, hi) == expected and snippet


@pytest.mark.parametrize("text", [
    "",
    "Great experience building teams.",
    "Over a period of 18 months, participants rotate through teams.",  # a programme length, not a requirement
    "We value your experience. 2 years ago we launched a new product.",
    "Join us for 12 months of mentoring.",
])
def test_missing_or_ambiguous_stays_unknown(text):
    assert parse_experience(text) == (None, None, None), "unknown is never turned into zero"


def test_explicit_zero_is_not_unknown():
    assert parse_experience("0 to 12 months of experience")[0] == 0.0
    assert parse_experience("no particular experience needed")[0] is None


def _check(db, desc):
    from jobpilot.services import jobs as job_svc

    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Software Engineer", company_name="Examplecorp",
                                                        location="Bengaluru", description_text=desc))
    return job, next(c for c in job.match_details["eligibility"]["checks"] if c["name"] == "experience")


def test_months_requirement_passes_for_one_year_of_experience(db):
    from tests.helpers import seed_profile

    seed_profile(db, years=1.0)
    job, check = _check(db, "Requirements:\n- 0 to 12 months of experience in software engineering\n- Java")
    assert check["status"] == "pass" and "requires 0+ years" in check["explanation"]


def test_reversed_wording_requirement_fails_with_a_hard_conflict(db):
    from tests.helpers import seed_profile

    seed_profile(db, years=1.0)
    job, check = _check(db, "Requirements:\n- Overall career experience of 8–12 years into Software Engineering in Java")
    assert check["status"] == "fail" and check["hard"] and "7 year(s) short" in check["explanation"]
    assert job.eligibility == "ineligible" and job.hard_conflict
