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
        "review_band_low": 40,
        "max_missing_mandatory_ratio": 0.5,
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
    review_band_low: int = Field(ge=0, le=100)
    max_missing_mandatory_ratio: float = Field(ge=0, le=1)


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
    "outreach": OutreachSettings.model_validate,
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
        audit.record(session, "settings.reset", "app_setting", None, key=key)
