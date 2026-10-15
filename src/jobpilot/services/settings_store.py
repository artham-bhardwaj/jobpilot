"""Runtime settings persisted in the ``app_settings`` table, with defaults."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy.orm import Session

from jobpilot.models import AppSetting
from jobpilot.services import audit

DEFAULTS: dict[str, Any] = {
    # Search preferences used for filtering and relevance scoring.
    "search_preferences": {
        "include_keywords": [],
        "exclude_keywords": ["principal", "staff engineer", "director", "vp ", "intern"],
        "locations": [],
        "remote_preferences": ["remote", "hybrid", "onsite"],
        "experience_levels": ["entry", "mid"],
        "min_salary": None,
        "salary_currency": None,
    },
    # Matching thresholds.
    "matching": {
        "relevance_threshold": 55,
        "strong_threshold": 75,
        "review_band_low": 40,
        "max_missing_mandatory_ratio": 0.5,
    },
    # Relative weights of the relevance components (normalised to 100). Change them yourself on the
    # Matching calibration page; feedback never changes them automatically.
    "matching_weights": {
        "required_skills": 30,
        "preferred_skills": 8,
        "role_alignment": 25,
        "experience_level": 12,
        "education": 5,
        "location": 12,
        "preferences": 8,
    },
    # Which failed checks count as *hard* conflicts (shown as "not suitable" whatever the relevance score).
    "hard_rules": {
        "experience_gap_years": 2.0,
        "sponsorship_conflict": True,
        "location_conflict": True,
        "excluded_title_keywords": True,
    },
    # When a job is sent to the review queue.
    "review_rules": {
        "strong_match": True,
        "relevant_but_eligibility_unknown": True,
        "eligibility_warnings": True,
        "borderline_relevance": True,
        "low_confidence_relevant": True,
        "partial_extraction": True,
    },
    # Your additions to the built-in skill dictionary (see Settings). Example:
    # {"extra": {"quarkus": {"category": "framework", "aliases": ["quarkus"]}}, "disabled": ["agile"]}
    "skill_dictionary": {"extra": {}, "disabled": []},
    # Browser assistance: hosted-form types where autofill may run (others open for manual completion).
    "browser": {"autofill_sites": ["greenhouse", "lever"]},
    # Job collection safety. A job expires only after this many consecutive successful snapshots of its
    # source omit it; an empty snapshot or a mass disappearance is treated as a source anomaly instead.
    "collection": {
        "expire_after_misses": 2,
        "anomaly_vanish_ratio": 0.5,
        "anomaly_min_active": 10,
    },
    # Outreach pacing. There is no bulk sending: every message is approved individually.
    "outreach": {
        "follow_up_days": 7,
        "max_follow_ups": 2,
        "daily_send_limit": 10,
    },
    # Optional scheduled tasks (run only while the computer is on). All off by default.
    "scheduler": {
        "keep_backups": 14,
        "tasks": {
            "collect_jobs": {"enabled": False, "every_hours": 24},
            "followup_check": {"enabled": False, "every_hours": 24},
            "maintenance": {"enabled": False, "every_hours": 24},
            "backup": {"enabled": False, "every_hours": 24},
            "gmail_sync": {"enabled": False, "every_hours": 6},
        },
    },
    # Role categories: id -> {label, title_keywords, skill_keywords}.
    "role_categories": {
        "java_backend": {
            "label": "Java Backend Engineer",
            "title_keywords": ["java", "backend", "back-end", "back end", "spring"],
            "skill_keywords": ["java", "spring boot", "spring", "hibernate", "microservices", "kafka", "sql"],
        },
        "sde1": {
            "label": "SDE I",
            "title_keywords": ["sde i", "sde 1", "sde-1", "software development engineer i", "software engineer i",
                               "engineer i", "new grad", "entry level", "junior", "graduate", "associate software"],
            "skill_keywords": ["data structures", "algorithms", "java", "python", "c++", "oop"],
        },
        "python_backend": {
            "label": "Python Backend",
            "title_keywords": ["python", "django", "fastapi", "flask", "backend", "back-end"],
            "skill_keywords": ["python", "django", "fastapi", "flask", "sql", "postgresql", "rest api"],
        },
        "ai_ml": {
            "label": "AI/ML",
            "title_keywords": ["machine learning", "ml engineer", "ai engineer", " ai ", "ai/ml", "applied scientist",
                               "data scientist", "deep learning", "nlp", "llm", "computer vision"],
            "skill_keywords": ["machine learning", "pytorch", "tensorflow", "deep learning", "nlp", "llm",
                               "scikit-learn", "python"],
        },
        "general_swe": {
            "label": "General SWE",
            "title_keywords": ["software engineer", "software developer", "swe", "developer", "programmer",
                               "software development engineer"],
            "skill_keywords": ["data structures", "algorithms", "git", "sql", "rest api"],
        },
    },
}


class SearchPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_keywords: list[str] = []
    exclude_keywords: list[str] = []
    locations: list[str] = []
    remote_preferences: list[Literal["remote", "hybrid", "onsite"]] = ["remote", "hybrid", "onsite"]
    experience_levels: list[Literal["entry", "mid", "senior"]] = ["entry", "mid"]
    min_salary: int | None = Field(default=None, ge=0)
    salary_currency: str | None = None


class MatchingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relevance_threshold: int = Field(ge=0, le=100)
    strong_threshold: int = Field(default=75, ge=0, le=100)
    review_band_low: int = Field(ge=0, le=100)
    max_missing_mandatory_ratio: float = Field(ge=0, le=1)


WEIGHT_KEYS = ("required_skills", "preferred_skills", "role_alignment", "experience_level", "education",
               "location", "preferences")


class MatchingWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_skills: float = Field(ge=0, le=100)
    preferred_skills: float = Field(ge=0, le=100)
    role_alignment: float = Field(ge=0, le=100)
    experience_level: float = Field(ge=0, le=100)
    education: float = Field(ge=0, le=100)
    location: float = Field(ge=0, le=100)
    preferences: float = Field(ge=0, le=100)


class HardRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experience_gap_years: float = Field(ge=0, le=20)
    sponsorship_conflict: bool
    location_conflict: bool
    excluded_title_keywords: bool


class ReviewRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strong_match: bool = True
    relevant_but_eligibility_unknown: bool = True
    eligibility_warnings: bool = True
    borderline_relevance: bool = True
    low_confidence_relevant: bool = True
    partial_extraction: bool = True


class SkillSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = Field(default=None, max_length=40)
    aliases: list[str] = Field(default_factory=list, max_length=30)


class SkillDictionary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra: dict[str, SkillSpec] = Field(default_factory=dict, max_length=500)
    disabled: list[str] = Field(default_factory=list, max_length=500)


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    autofill_sites: list[Literal["greenhouse", "lever"]] = Field(default_factory=list)


class CollectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expire_after_misses: int = Field(ge=1, le=10)
    anomaly_vanish_ratio: float = Field(gt=0, le=1)
    anomaly_min_active: int = Field(ge=1, le=10_000)


class OutreachSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    follow_up_days: int = Field(ge=1, le=60)
    max_follow_ups: int = Field(ge=0, le=3)
    daily_send_limit: int = Field(ge=1, le=25)


class ScheduledTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    every_hours: float = Field(default=24, ge=1, le=24 * 30)


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_backups: int = Field(default=14, ge=1, le=365)
    tasks: dict[Literal["collect_jobs", "followup_check", "maintenance", "backup", "gmail_sync"], ScheduledTask] = {}


class RoleCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    title_keywords: list[str]
    skill_keywords: list[str] = []


_VALIDATORS = {
    "search_preferences": SearchPreferences.model_validate,
    "matching": MatchingSettings.model_validate,
    "matching_weights": MatchingWeights.model_validate,
    "hard_rules": HardRules.model_validate,
    "review_rules": ReviewRules.model_validate,
    "outreach": OutreachSettings.model_validate,
    "collection": CollectionSettings.model_validate,
    "browser": BrowserSettings.model_validate,
    "skill_dictionary": SkillDictionary.model_validate,
    "scheduler": SchedulerSettings.model_validate,
    "role_categories": lambda v: {k: RoleCategory.model_validate(c) for k, c in TypeAdapter(dict).validate_python(v).items()},
}


def validate(key: str, value: Any) -> None:
    """Raise ValidationError/ValueError if ``value`` is not acceptable for ``key``."""
    if key not in _VALIDATORS:
        raise ValueError(f"unknown setting {key}")
    _VALIDATORS[key](value)
    if key == "role_categories" and not value:
        raise ValueError("at least one role category is required")
    if key == "matching_weights" and sum(float(v) for v in value.values()) <= 0:
        raise ValueError("at least one weight must be positive")
    if key == "matching" and value["review_band_low"] > value["relevance_threshold"]:
        raise ValueError("review_band_low must not exceed relevance_threshold")


def get(session: Session, key: str) -> Any:
    row = session.get(AppSetting, key)
    default = copy.deepcopy(DEFAULTS.get(key))
    if row is None:
        return default
    if isinstance(default, dict) and isinstance(row.value, dict):
        merged = default
        merged.update(row.value)
        return merged
    return row.value


def put(session: Session, key: str, value: Any) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    audit.record(session, "settings.updated", "app_setting", None, key=key)


def reset(session: Session, key: str) -> None:
    row = session.get(AppSetting, key)
    if row is not None:
        session.delete(row)
        session.flush()  # so reads in this session immediately see the default again
        audit.record(session, "settings.reset", "app_setting", None, key=key)
