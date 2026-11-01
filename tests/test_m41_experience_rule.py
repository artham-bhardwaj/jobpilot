"""The experience requirement is the highest figure you must meet (a real posting stated 7+ years first and 2+ years
for CI/CD last, and was stored as 2 years, so it passed as fresher / SDE-1)."""

from __future__ import annotations

import pytest

from jobpilot.services import cleanup, levels
from jobpilot.services import jobs as job_svc
from jobpilot.services.job_parser import parse_experience
from jobpilot.sources.base import JobData

REPORTED = """7+ years' experience in software engineering from ideation to production deployment of IT solutions
5+ years' experience in full software development life cycle including ideation, coding, coding standards, testing, code reviews and production deployments
5+ years of experience with backend Java , Spring Boot and Microservices
3+ years of hands-on experience with a public cloud provider
3+ years working with pub/sub tools like Kafka or similar
3+ years of experience with database design/development (Postgres or similar)
2+ years of experience with CI/CD tools (GitHub Actions, Jenkins, Argo CD, or similar)"""

AMAZON_SDE1 = """Basic Qualifications
- 2+ years of non-internship professional software development experience
- Experience programming with at least one software programming language
Preferred Qualifications
- 3+ years of full software development life cycle, including coding standards, code reviews, source control management, build processes, testing, and operations experience"""

VISA_INLINE = ("Visa requires at least 3 days in office. Qualifications Basic Qualifications: 7+ years of relevant work "
               "experience with a Bachelor’s Degree or at least 2 years of work experience with an Advanced degree "
               "(e.g. Masters, MBA, JD, MD) or 0 years of work experience with a PhD, OR 8+ years of relevant work "
               "experience. Preferred Qualifications: 5+ years of relevant work experience with a Bachelor’s Degree")


@pytest.mark.parametrize("text,expected", [
    (REPORTED, 7.0),
    (AMAZON_SDE1, 2.0),  # the higher figure is only preferred
    (VISA_INLINE, 7.0),  # inline headings; degree alternatives; "e.g." isn't a sentence end
    ("- 3+ years of computer science fundamentals (object-oriented design, data structures, algorithm design, "
     "problem solving and complexity analysis) experience", 3.0),  # long qualifier in a bullet
    ("3+ years working with pub/sub tools like Kafka", 3.0),
    ("3+ years of experience with Java or 5+ years of experience in any language", 3.0),  # alternatives
    ("5+ years of experience in management is valued; 2+ years of experience with Python required.", 2.0),
    ("Requirements: 1+ years of experience in Java. Ideally 4+ years of experience with Kafka.", 1.0),
    ("Experience with AWS (5+ years) is a plus. 1+ years of experience in Java.", 1.0),
    ("With over 20 years of experience, our company leads the market. Requirements: 1+ years of experience in Java.", 1.0),
    ("We are looking for engineers with 2+ years of experience.", 2.0),
    ("7+ years of hands-on software engineering experience, with a strong track record of delivery", 7.0),
    ("Preferred: 3+ years of experience with Go", 3.0),  # only a preferred figure: still the best information
])
def test_requirement_is_the_highest_required_figure(text, expected):
    assert parse_experience(text)[0] == expected


def test_reported_posting_is_above_sde1_and_hidden(db):
    job, _ = job_svc.upsert(db, None, JobData(title="Java developer | Offshore | Chennai/Bangalore",
                                              company_name="Examplecorp", location="Remote — India",
                                              description_text=REPORTED, source_url="https://example.com/j"), "manual")
    assert job.experience_min_years == 7.0 and job.level_fit == "no" and levels.of(job) == "no"
    job.category, job.relevance_score, job.hard_conflict = "java_backend", 94.0, True
    db.commit()
    assert cleanup.reasons(job, 40) == ["level", "conflict"]
    assert job.id not in {j.id for j in job_svc.search(db, level="fresher", region="mine", roles_only=True)[0]}


def test_hard_conflict_alone_hides_a_job(db):
    job, _ = job_svc.upsert(db, None, JobData(title="Backend Engineer", company_name="Examplecorp",
                                              location="Pune, India", description_text="Java (synthetic).",
                                              source_url="https://example.com/k"), "manual")
    job.category, job.relevance_score, job.hard_conflict = "java_backend", 90.0, True
    db.commit()
    assert cleanup.run(db)["hidden"] == 1 and job.hidden_reason == "cleanup:conflict"
    assert cleanup.label(job.hidden_reason) == "not suitable (hard conflict)"
