"""Milestone 10: requirement qualifiers (examples, interest, familiarity…) and source deadlines.

Offline, synthetic text only. The phrasing mirrors gaps seen on the first live Greenhouse board.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from jobpilot.models import Job, utcnow
from jobpilot.services import job_parser
from jobpilot.services import jobs as job_svc
from jobpilot.sources.base import parse_deadline
from tests.helpers import make_http, seed_profile


def skills(text: str) -> dict[str, tuple[str, str]]:
    return {k: (v["kind"], v["qualifier"]) for k, v in job_parser.parse("Engineer", text).skill_evidence.items()}


# ---- Examples are not requirements ---------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Requirements:\n- Deep experience with Ruby on Rails, or comparable depth in another framework's migration system, "
     "such as Django or Ecto.", {"ruby": ("required", "requirement"), "django": ("preferred", "example")}),
    ("Requirements:\n- Experience with cloud platforms (e.g. AWS, GCP) and strong Python skills",
     {"aws": ("preferred", "example"), "gcp": ("preferred", "example"), "python": ("required", "requirement")}),
    ("Requirements:\n- Python experience, and web frameworks for example Django; strong SQL",
     {"python": ("required", "requirement"), "django": ("preferred", "example"), "sql": ("required", "requirement")}),
    ("Nice to have:\n- Message queues such as Kafka", {"kafka": ("preferred", "example")}),
    ("About us: we build tools such as Kafka connectors.\nRequirements:\n- Java", {"kafka": ("mentioned", "example"),
                                                                                 "java": ("required", "requirement")}),
])
def test_examples_are_kept_but_never_mandatory(text, expected):
    assert skills(text) == expected


@pytest.mark.parametrize("text", [
    "Requirements:\n- Proficiency in Django",
    "Requirements:\n- Experience in Django, including its ORM",
    "Requirements:\n- Django. We also like clean code.",
])
def test_plain_requirements_without_example_wording_stay_required(text):
    assert skills(text)["django"] == ("required", "requirement")


# ---- Interest, familiarity, exposure, preference, requirement -------------------------------

@pytest.mark.parametrize("text,skill,expected", [
    ("Requirements:\n- Interest or experience in applying AI/ML to detection workflows", "machine learning", ("preferred", "interest")),
    ("Requirements:\n- A passion for Python", "python", ("preferred", "interest")),
    ("Requirements:\n- Willingness to learn Go", "go", ("preferred", "interest")),
    ("Requirements:\n- Familiarity with Kubernetes", "kubernetes", ("preferred", "familiarity")),
    ("Requirements:\n- Exposure to Docker", "docker", ("preferred", "exposure")),
    ("Requirements:\n- Experience with Kafka is preferred but not required", "kafka", ("preferred", "preference")),
    ("Requirements:\n- Familiarity with Git is required", "git", ("required", "requirement")),
    ("Requirements:\n- You must have hands-on Terraform experience", "terraform", ("required", "requirement")),
])
def test_strength_of_each_ask_is_distinguished(text, skill, expected):
    assert skills(text)[skill] == expected


def test_interest_span_does_not_swallow_a_following_requirement():
    got = skills("Requirements:\n- Strong interest in Go, and 3+ years of experience with Java")
    assert got == {"go": ("preferred", "interest"), "java": ("required", "requirement")}


# ---- One skill, several contexts: the strongest real ask wins, wherever it appears ---------

@pytest.mark.parametrize("text", [
    "About us: our tech stack uses Django.\nRequirements:\n- Django experience",
    "Requirements:\n- Django experience\nAbout us: our tech stack uses Django.",
    "Requirements:\n- Frameworks such as Django\n- Strong Django skills",
    "Requirements:\n- Strong Django skills\n- Frameworks such as Django",
])
def test_required_context_beats_mention_or_example_in_either_order(text):
    p = job_parser.parse("Engineer", text)
    assert p.skill_evidence["django"]["kind"] == "required" and "django" in p.mandatory_skills
    assert "such as" not in p.skill_evidence["django"]["snippet"] and "stack" not in p.skill_evidence["django"]["snippet"]


def test_mention_only_stays_mentioned():
    p = job_parser.parse("Engineer", "About us: our tech stack uses Django.\nRequirements:\n- Java")
    assert "django" in p.mentioned_skills and "django" not in p.mandatory_skills + p.preferred_skills


# ---- Safeguards still hold ------------------------------------------------------------------

def test_examples_do_not_create_missing_mandatory_skills_or_hide_hard_conflicts(db):
    seed_profile(db, skills=("ruby",), learning=(), needs_sponsorship=True)
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(
        title="Backend Engineer", company_name="Examplecorp", location="Bengaluru",
        description_text="Requirements:\n- Ruby on Rails, or a comparable framework such as Django or Ecto\n"
                         "We are unable to sponsor visas."))
    sk = job.match_details["skills"]
    assert sk["mandatory"] == ["ruby"] and "django" in sk["preferred"] and sk["missing_mandatory"] == []
    assert job.hard_conflict and job.match_details["recommendation"]["key"] == "not_suitable"


# ---- Source deadlines: strict, with provenance, never guessed ------------------------------

@pytest.mark.parametrize("value,expected", [
    ("2026-11-30", datetime(2026, 11, 30, 23, 59, 59)),  # a date means the end of that day
    ("2026-11-30T17:00:00Z", datetime(2026, 11, 30, 17, 0, 0)),
    ("2026-11-30T23:00:00+05:30", datetime(2026, 11, 30, 17, 30, 0)),
    ("2026-11-30T23:00:00.000-08:00", datetime(2026, 12, 1, 7, 0, 0)),
])
def test_valid_deadlines(value, expected):
    assert parse_deadline(value) == (expected, None)


@pytest.mark.parametrize("value", ["next Friday", "30/11/2026", "Nov 30, 2026", "2026-11", "2026-13-01", "2026-02-30",
                                   "1970-01-01", "2999-01-01", 1764460800, "2026-11-30 because", " "])
def test_invalid_or_vague_deadlines_are_ignored_with_a_reason(value):
    deadline, problem = parse_deadline(value)
    assert deadline is None and problem


def test_missing_deadline_is_simply_unknown():
    assert parse_deadline(None) == (None, None) and parse_deadline("") == (None, None)


def test_deadline_before_the_posting_date_is_ignored():
    assert parse_deadline("2026-01-01", posted_at=datetime(2026, 3, 1))[0] is None
    assert parse_deadline("2026-03-01", posted_at=datetime(2026, 3, 1, 12))[0] is not None  # same day is fine


GH = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


def _board(deadlines: dict[int, object]) -> str:
    return json.dumps({"jobs": [{"id": i, "title": f"Engineer {i}", "absolute_url": f"https://job-boards.greenhouse.io/examplecorp/jobs/{i}",
                                 "location": {"name": "Pune"}, "content": "Requirements: Java", "first_published": "2026-10-01T00:00:00Z",
                                 "application_deadline": d} for i, d in deadlines.items()]})


def _collect(db, settings, deadlines):
    return job_svc.collect_all(db, settings, make_http(settings, {GH: (200, _board(deadlines))}))["sources"]["Examplecorp"]


@pytest.fixture
def gh_source(db):
    return job_svc.save_source(db, job_svc.SourceIn(name="Examplecorp", kind="greenhouse", company_name="Examplecorp",
                                                    config={"board_token": "examplecorp"}))


def test_greenhouse_application_deadline_is_parsed_with_provenance(db, settings, gh_source):
    _collect(db, settings, {1: "2030-11-30", 2: "end of month", 3: None})
    jobs = {j.external_id: j for j in db.scalars(select(Job))}
    good, bad, none = jobs["1"], jobs["2"], jobs["3"]
    assert good.valid_through == datetime(2030, 11, 30, 23, 59, 59)
    assert good.raw["application_deadline"] == "2030-11-30" and good.raw["deadline_source"] == "Greenhouse application_deadline"
    dl = good.match_details["requirements"]["evidence"]["deadline"]
    assert dl == {"value": "2030-11-30", "basis": "structured", "snippet": "source data (Greenhouse application_deadline)"}
    assert bad.valid_through is None and bad.raw["application_deadline"] == "end of month"
    assert "not an ISO date" in bad.raw["deadline_ignored"] and "deadline_source" not in bad.raw
    assert bad.match_details["requirements"]["evidence"]["deadline"]["basis"] == "unknown"
    assert none.valid_through is None and "application_deadline" not in none.raw


def test_deadline_added_later_updates_the_job_and_reruns_stay_stable(db, settings, gh_source):
    assert _collect(db, settings, {1: None})["new"] == 1
    job = db.scalar(select(Job))
    legacy = job_svc.sha256("|".join(str(x or "") for x in (job.title, job.location, job.description_text, None, None)))
    assert job.content_hash == legacy, "jobs without a deadline keep their existing hash (no spurious updates)"
    assert _collect(db, settings, {1: "2030-06-30"})["updated"] == 1
    assert job.valid_through == datetime(2030, 6, 30, 23, 59, 59)
    assert _collect(db, settings, {1: "2030-06-30"})["unchanged"] == 1
    assert _collect(db, settings, {1: None})["updated"] == 1, "a deadline the source removes is cleared too"
    assert job.valid_through is None and job.content_hash == legacy


def test_only_clearly_passed_deadlines_expire_jobs(db, settings, gh_source):
    now = utcnow()
    _collect(db, settings, {1: None, 2: None, 3: None, 4: None, 5: None})
    jobs = {j.external_id: j for j in db.scalars(select(Job))}
    jobs["1"].valid_through = now - timedelta(days=2)  # clearly passed everywhere
    jobs["2"].valid_through = now - timedelta(hours=2)  # passed in UTC, maybe not in the posting's time zone
    jobs["3"].valid_through = now + timedelta(days=5)
    jobs["1"].raw = {**jobs["1"].raw, "deadline_source": "Greenhouse application_deadline"}
    db.flush()
    assert job_svc.expire_past_deadline(db) == 1
    assert [j.status for _, j in sorted(jobs.items())] == ["expired", "active", "active", "active", "active"]


def test_jsonld_valid_through_is_strict_too():
    from jobpilot.sources.pages import posting_to_job

    base = {"@type": "JobPosting", "title": "Engineer", "datePosted": "2026-10-01", "description": "Requirements: Java"}
    good = posting_to_job({**base, "validThrough": "2030-12-31"}, "https://careers.example.com/jobs/1")
    vague = posting_to_job({**base, "validThrough": "soon"}, "https://careers.example.com/jobs/2")
    assert good.valid_through == datetime(2030, 12, 31, 23, 59, 59) and good.raw["deadline_source"] == "schema.org validThrough"
    assert vague.valid_through is None and "deadline_source" not in vague.raw


@pytest.mark.parametrize("deadline,still_active_at,expired_at", [
    # Date only: the end of 30 Nov, plus 14 h for the posting's unknown time zone.
    ("2030-11-30", datetime(2030, 12, 1, 13, 59, 59), datetime(2030, 12, 1, 14, 0, 0)),
    # Explicit offset: converted to UTC first (17:30 UTC), then the same grace.
    ("2030-11-30T23:00:00+05:30", datetime(2030, 12, 1, 7, 30, 0), datetime(2030, 12, 1, 7, 30, 1)),
])
def test_structured_deadline_expiry_boundary(db, settings, gh_source, monkeypatch, deadline, still_active_at, expired_at):
    _collect(db, settings, {1: deadline})
    job = db.scalar(select(Job))
    monkeypatch.setattr("jobpilot.services.jobs.utcnow", lambda: still_active_at)
    assert job_svc.expire_past_deadline(db) == 0 and job.status == "active"
    monkeypatch.setattr("jobpilot.services.jobs.utcnow", lambda: expired_at)
    assert job_svc.expire_past_deadline(db) == 1 and job.status == "expired"


def test_text_deadline_expiry_boundary(db, monkeypatch):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title="Role", description_text="Apply by 30 November 2030."))
    assert job.match_details["requirements"]["evidence"]["deadline"]["value"] == "2030-11-30"
    monkeypatch.setattr("jobpilot.services.jobs.utcnow", lambda: datetime(2030, 12, 1, 13, 59, 59))
    assert job_svc.expire_past_deadline(db) == 0 and job.status == "active"
    monkeypatch.setattr("jobpilot.services.jobs.utcnow", lambda: datetime(2030, 12, 1, 14, 0, 0))
    assert job_svc.expire_past_deadline(db) == 1 and job.status == "expired"
