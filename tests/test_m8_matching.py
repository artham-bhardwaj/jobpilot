"""Milestone 8 — Stage D: parsing provenance/regressions, matching components, confidence, hard conflicts,
configurable weights, calibration (offline; synthetic data)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from jobpilot.models import Job, MatchFeedback
from jobpilot.services import calibration, job_parser, matching, settings_store
from jobpilot.services import jobs as job_svc
from jobpilot.services import profile as ps
from tests.helpers import FIXTURES, seed_profile

CASES = json.loads((FIXTURES / "parsing_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_parsing_regressions(case):
    p = job_parser.parse(case["title"], case["text"])
    e = case["expect"]
    for key in ("experience_min_years", "remote_type", "sponsorship", "salary_min", "salary_max", "salary_currency",
                "degree_equivalent_experience_ok", "application_deadline", "employment_type", "experience_level",
                "degree_required"):
        if key in e:
            assert getattr(p, key) == e[key], (key, getattr(p, key))
    if "mandatory" in e:
        assert sorted(p.mandatory_skills) == sorted(e["mandatory"])
    for s in e.get("mandatory_includes", []):
        assert s in p.mandatory_skills, (s, p.skill_evidence)
    if "preferred" in e:
        assert sorted(p.preferred_skills) == sorted(e["preferred"])
    for s in e.get("mentioned_includes", []):
        assert s in p.mentioned_skills and s not in p.mandatory_skills
    for g in e.get("alternative_groups", []):
        assert g in p.alternatives
    for s, basis in e.get("basis", {}).items():
        assert p.skill_evidence[s]["basis"] == basis
    for s in e.get("not_skills", []):
        assert s not in p.all_skills
    if e.get("degree_required_present"):
        assert p.degree_required


def test_parse_records_provenance_and_unknowns():
    p = job_parser.parse("Engineer", "Requirements: 3+ years of experience with Java. Hybrid.",
                         structured={"remote_type": "remote", "employment_type": "Full-time"})
    assert p.evidence["work_mode"] == {"value": "remote", "basis": "structured", "snippet": "source data"}
    assert p.evidence["experience"]["basis"] == "explicit" and "3+ years" in p.evidence["experience"]["snippet"]
    assert p.evidence["seniority"]["basis"] == "inferred"
    assert p.evidence["salary"]["basis"] == "unknown" and p.evidence["deadline"]["value"] is None
    assert p.skill_evidence["java"]["kind"] == "required" and p.skill_evidence["java"]["basis"] == "explicit"


def test_configurable_skill_dictionary(db):
    text = "Requirements: Quarkus and Java. Agile team."
    assert "quarkus" not in job_parser.parse("x", text).all_skills
    settings_store.put(db, "skill_dictionary", {"extra": {"quarkus": {"category": "framework", "aliases": ["quarkus.io"]}},
                                                "disabled": ["agile"]})
    from jobpilot.services import skills_taxonomy

    skills_taxonomy.configure_from_settings(db)
    p = job_parser.parse("x", text)
    assert "quarkus" in p.mandatory_skills and "agile" not in p.all_skills
    with pytest.raises(Exception):
        settings_store.validate("skill_dictionary", {"extra": {"x": {"bogus": 1}}})
    settings_store.reset(db, "skill_dictionary")
    skills_taxonomy.configure_from_settings(db)
    assert "agile" in job_parser.parse("x", text).all_skills


# ---- Matching -----------------------------------------------------------------------------------

def _job(db, title, desc, **kw):
    job, _ = job_svc.add_manual(db, job_svc.ManualJobIn(title=title, company_name="Examplecorp",
                                                        location=kw.pop("location", "Bengaluru"), description_text=desc, **kw))
    return job


def test_components_have_evidence_and_weights_sum_to_100(db):
    seed_profile(db)
    job = _job(db, "Java Backend Engineer", "Requirements:\n- 1+ years of experience with Java\n- Spring Boot\n- SQL",
               remote_type="hybrid")
    rel = job.match_details["relevance"]
    assert round(sum(c["max"] for c in rel["components"]), 1) == 100.0
    req = next(c for c in rel["components"] if c["key"] == "required_skills")
    assert any("java — required (explicit, requirements section" in e and "confirmed (work: Software Engineer at Synthetic Corp)" in e
               for e in req["evidence"]), req["evidence"]
    assert job.match_details["recommendation"]["key"] in ("strong", "relevant")
    assert job.confidence in ("medium", "high")


def test_unverified_learning_and_missing_are_distinguished(db):
    seed_profile(db, skills=("java",), learning=("kafka",))
    ps.save_fact(db, "skills", ps.SkillIn(name="Docker", status="unverified"))
    job = _job(db, "Java Developer", "Requirements:\n- Java\n- Kafka\n- Docker\n- Redis")
    sk = job.match_details["skills"]
    assert sk["have"] == ["java"] and sk["learning"] == ["kafka"] and sk["unverified"] == ["docker"]
    assert set(sk["missing_mandatory"]) == {"kafka", "docker", "redis"}, "only confirmed skills count as met"
    check = next(c for c in job.match_details["eligibility"]["checks"] if c["name"] == "required skills")
    assert "docker listed in your profile but unverified" in check["explanation"]
    assert "UNVERIFIED" in sk["candidate"]["docker"] and "learning" in sk["candidate"]["kafka"]
    assert sk["candidate"]["redis"] == "not in your profile"


def test_work_experience_vs_personal_projects(db):
    seed_profile(db, skills=("java", "kafka"), learning=())
    ps.save_fact(db, "projects", ps.ProjectIn(name="Event pipeline", technologies="Kafka", status="confirmed"))
    job = _job(db, "Java Developer", "Requirements:\n- Java\n- Kafka")
    cand = job.match_details["skills"]["candidate"]
    assert "work: Software Engineer at Synthetic Corp" in cand["java"]
    assert "personal projects: Event pipeline" in cand["kafka"] and "work:" not in cand["kafka"]
    exp = next(c for c in job.match_details["eligibility"]["checks"] if c["name"] == "experience")
    assert exp["status"] == "unknown"  # posting doesn't state years: never assumed satisfied


def test_hard_conflict_overrides_high_relevance(db):
    seed_profile(db, needs_sponsorship=True)
    job = _job(db, "Java Backend Engineer", "Requirements:\n- 1+ years of experience with Java\n- Spring Boot\n- SQL\n"
               "We are unable to sponsor visas.", remote_type="hybrid")
    assert job.relevance_score >= 60, "relevance itself stays high"
    assert job.hard_conflict and job.match_details["recommendation"]["key"] == "not_suitable"
    assert job.match_details["relevance"]["relevant"] is False
    assert job.match_details["needs_review"] is False, "hard conflicts don't clutter the review queue"


def test_soft_gaps_are_not_hard_conflicts(db):
    seed_profile(db, skills=("java",))
    job = _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- Kafka\n- Redis\n- Docker\n- 2+ years of experience")
    assert job.eligibility == "ineligible" and not job.hard_conflict
    assert job.match_details["recommendation"]["key"] == "stretch"


def test_experience_gap_hardness_is_configurable(db):
    seed_profile(db, years=1.0)
    desc = "Requirements:\n- 4+ years of experience with Java\n- SQL"
    j1 = _job(db, "Java Developer", desc)
    assert j1.hard_conflict  # 3 years short > default 2
    settings_store.put(db, "hard_rules", {**settings_store.get(db, "hard_rules"), "experience_gap_years": 5})
    matching.match_job(db, j1)
    assert not j1.hard_conflict and j1.eligibility == "ineligible"


def test_missing_optional_info_does_not_reject(db):
    seed_profile(db)
    job = _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- Spring Boot\n- SQL", location=None)
    assert job.eligibility in ("unknown", "likely_eligible") and not job.hard_conflict
    assert job.match_details["recommendation"]["key"] in ("strong", "relevant")
    assert "years of experience required are not stated" in json.dumps(job.match_details["eligibility"])


def test_low_information_posting_has_low_confidence_and_no_free_relevance(db):
    seed_profile(db)
    job = _job(db, "Office Manager", "Join us!", location=None)
    assert job.confidence == "low" and job.relevance_score < 40
    assert job.match_details["recommendation"]["key"] == "low"


def test_weights_change_scores_only_when_saved_and_rescored(db):
    seed_profile(db)
    job = _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- Kafka\n- Redis", remote_type="hybrid")
    before = job.relevance_score
    w = {**settings_store.get(db, "matching_weights"), "required_skills": 80}
    preview = calibration.preview(db, [job], w)[job.id]["score"]
    assert preview != before and job.relevance_score == before, "preview never writes"
    calibration.save_weights(db, w)
    assert job.relevance_score == before, "saving weights doesn't silently rescore"
    matching.match_job(db, job)
    assert job.relevance_score == preview
    with pytest.raises(Exception):
        calibration.save_weights(db, {k: 0 for k in w})


def test_feedback_is_recorded_but_never_changes_scoring(db):
    seed_profile(db)
    job = _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- SQL", remote_type="hybrid")
    weights_before, score_before = settings_store.get(db, "matching_weights"), job.relevance_score
    for _ in range(3):
        calibration.record_feedback(db, job, "irrelevant", "synthetic note")
    assert settings_store.get(db, "matching_weights") == weights_before and job.relevance_score == score_before
    fb = db.scalar(select(MatchFeedback))
    assert fb.score == score_before and fb.weights == weights_before
    rep = calibration.report(db)
    assert rep["irrelevant"] == 1 and rep["enough_data"] is False and job in rep["irrelevant_but_scored_relevant"]
    with pytest.raises(ValueError):
        calibration.record_feedback(db, job, "meh")


def test_calibration_page_preview_and_save_via_web(client, db):
    seed_profile(db)
    job = _job(db, "Java Backend Engineer", "Requirements:\n- Java\n- Kafka", remote_type="hybrid")
    db.commit()
    page = client.get("/matching?mode=top&w_required_skills=70")
    assert page.status_code == 200 and "Save previewed weights" in page.text
    db.expire_all()
    assert settings_store.get(db, "matching_weights")["required_skills"] == 30, "GET preview must not save"
    r = client.post(f"/matching/feedback/{job.id}", data={"verdict": "useful"})
    assert "msg=" in r.headers["location"]
    assert "err=" in client.post(f"/matching/feedback/{job.id}", data={"verdict": "nope"}).headers["location"]
    assert client.post("/matching/feedback/999999", data={"verdict": "useful"}).status_code == 404
    weights = {f"w_{k}": v for k, v in settings_store.get(db, "matching_weights").items()}
    r = client.post("/matching/weights", data={**weights, "w_required_skills": "45"})
    assert "msg=" in r.headers["location"]
    db.expire_all()
    assert settings_store.get(db, "matching_weights")["required_skills"] == 45
    bad = client.post("/matching/weights", data={k: "0" for k in weights})
    assert "err=" in bad.headers["location"]
