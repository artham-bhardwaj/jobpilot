"""Runtime settings persisted in the ``app_settings`` table, with defaults."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
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
    # Browser assistance (you click Submit yourself): hosted-form types where autofill may run (others open for
    # manual completion); use_answer_bank: also fill approved, non-sensitive answer-bank answers.
    "browser": {"autofill_sites": ["greenhouse", "lever"], "use_answer_bank": True},
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
        # Automatic recruiter outreach (scheduler task "outreach"). Off by default. auto_enabled: queue recruiter
        # discovery and draft emails for jobs that pass min_score / allowed_eligibility / job_families. auto_send:
        # also send them through Gmail, only to addresses YOU verified, at most auto_daily_limit per day. Never
        # follow-ups. JobPilot never applies to jobs for you.
        "auto_enabled": False,
        "auto_send": False,
        "auto_daily_limit": 3,
        "max_job_age_days": 30,
        "min_score": 70,
        "allowed_eligibility": ["eligible", "likely_eligible"],
        "job_families": ["java_backend", "sde1", "general_swe", "python_backend"],
    },
    # Your application tracker. dream_companies are marked ⭐ and listed first. cooldown_days: per company, the days
    # to wait between applications — set them yourself (companies rarely publish one; none is assumed).
    # ask_daily: how many "Did you apply?" questions Telegram asks per day (only if the apply_check notification is
    # on; the My tracker page asks the same in the dashboard); ask_again_days: after "Not yet".
    # auto_outcomes: an employer email with an unambiguous subject (rejection, assessment, interview, offer) updates
    # the application by itself (with an Undo button); anything less clear is asked about.
    "tracker": {
        "dream_companies": ["Meta", "Facebook", "Apple", "Amazon", "Netflix", "Google", "Alphabet"],
        "cooldown_days": {},
        "ask_daily": 5,
        "ask_again_days": 3,
        "auto_outcomes": True,
    },
    # Job-specific resumes from the approved Master resume's LaTeX source (services/tailored.py), made only when you
    # ask for one on a job's page (never automatically). Generation re-orders approved, confirmed content only.
    # attach: upload a job's validated tailored resume instead of the approved resume it was made from.
    # page_target: max pages (null = the Master resume's page count).
    "tailored_resumes": {
        "enabled": True,
        "attach": True,
        "page_target": None,
        "compile_timeout_s": 90,
        "min_description_chars": 400,
        "template": None,
        # Use the posting's spelling of skills you already list (pure synonyms only, checked by the validator).
        "reword_skills": True,
    },
    # The four agents (Search, Resume, Apply, Manager): see docs/AGENTS.md. The Manager runs them as the
    # scheduler task "agents". apply_mode "dry_run" does everything except send or submit; "auto" applies.
    # Relevance cleanup (services/cleanup.py): hide jobs that aren't fresher / SDE-1, in India / Dubai / remote, in
    # your role categories and at least min_score. Reversible; never touches jobs you applied to, saved or dismissed.
    # auto: also run after every collection, so irrelevant new jobs never reach the Jobs page, agents or Telegram.
    # sde_backend_only: also hide jobs whose title isn't an SDE / backend role (services/roles.py).
    "job_cleanup": {"auto": True, "min_score": 40, "sde_backend_only": True},
    "agents": {
        "apply_mode": "dry_run",
        "min_score": 70,
        "allowed_eligibility": ["eligible", "likely_eligible"],
        "job_families": [],
        "max_job_age_days": 30,
        "max_per_cycle": 5,
        "daily_limit": 10,
        "email_applications": True,
        "form_submissions": True,
        "allowed_platforms": ["greenhouse", "lever", "ashby"],
        "use_answer_bank": True,
        "hold_dream_companies": True,
        "fallback_to_master_resume": True,
        "search_every_hours": 6,
        "add_feed_sources": True,
        "discover_boards": True,
        "max_board_checks": 10,
        "job_alert_emails": True,
        "digest": True,
    },
    "apollo": {
        "enabled": False,                  # API mode off until you turn it on (and set JOBPILOT_APOLLO_API_KEY)
        "max_enrichments_per_day": 0,      # People Enrichment costs ~1 credit per email found; 0 = never
        "recruiter_titles": ["technical recruiter", "talent acquisition", "recruiter", "university recruiter",
                             "campus recruiter", "talent partner"],
        "country": "India",
    },
    "notifications": {
        "enabled": True,
        # Which events to send. Turned-off kinds are recorded as seen, so they are not sent later either.
        # Telegram is for alerts; day-to-day work happens in the dashboard, so the chattier kinds are off by default.
        "kinds": {"high_match": True, "relevant_digest": False, "draft_review": False, "application_status": True,
                  "reply": True, "follow_up": True, "daily_summary": True, "task_failure": True, "source_failure": True,
                  "questions": False, "recruiter_found": False, "outreach_gmail": True, "bounce": True,
                  "recruiter_needed": False, "outreach_sent": True, "apply_check": False, "email_outcome": True,
                  "agents_digest": True},
        # null = use the matching settings (relevance_threshold / strong_threshold).
        "relevant_min_score": None,
        "high_match_min_score": None,
        "daily_summary_hour": 20,  # local time; sent once per day at the first scan after this hour
        "max_items_per_message": 10,
        "max_attempts": 5,
    },
    "scheduler": {
        "keep_backups": 14,
        "tasks": {
            "collect_jobs": {"enabled": False, "every_hours": 24},
            "followup_check": {"enabled": False, "every_hours": 24},
            "maintenance": {"enabled": False, "every_hours": 24},
            "backup": {"enabled": False, "every_hours": 24},
            "gmail_sync": {"enabled": False, "every_hours": 6},
            "rematch": {"enabled": False, "every_hours": 24},
            "reminders": {"enabled": False, "every_hours": 12},
            "health_check": {"enabled": False, "every_hours": 24},
            # Scan for new events and deliver Telegram notifications (only when Telegram is configured).
            "notify": {"enabled": False, "every_hours": 0.25},
            "outreach": {"enabled": False, "every_hours": 6},
            # The Manager agent's cycle: search → tailored resumes → apply (or hand to you). See docs/AGENTS.md.
            "agents": {"enabled": False, "every_hours": 3},
        },
        # Reminders: how early to warn about a stated deadline, and when a submitted application with no
        # recorded update is worth checking on (never treated as a rejection).
        "deadline_warning_days": 3,
        "stale_application_days": 21,
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
                               "software dev engineer i", "engineer i", "new grad", "entry level", "junior", "graduate",
                               "associate software"],
            # A level, not a technology: only the title can say a job is SDE I ("C++, OOP" in a senior posting can't).
            "title_required": True,
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
                               "software development engineer", "software dev engineer", "sde"],
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
    autofill_sites: list[Literal["greenhouse", "lever", "ashby"]] = Field(default_factory=list)
    use_answer_bank: bool = True


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
    auto_enabled: bool = False
    auto_send: bool = False
    auto_daily_limit: int = Field(default=3, ge=0, le=10)
    max_job_age_days: int = Field(default=30, ge=1, le=120)
    min_score: float = Field(default=70, ge=0, le=100)
    allowed_eligibility: list[Literal["eligible", "likely_eligible", "unknown"]] = ["eligible", "likely_eligible"]
    job_families: list[str] = []


class ScheduledTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    every_hours: float = Field(default=24, ge=0.08, le=24 * 30)


NOTIFY_KINDS = Literal["high_match", "relevant_digest", "draft_review", "application_status", "reply", "follow_up",
                       "daily_summary", "task_failure", "source_failure", "questions", "recruiter_found",
                       "outreach_gmail", "bounce", "recruiter_needed", "outreach_sent", "apply_check",
                       "email_outcome", "agents_digest"]


class TailoredResumeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    attach: bool = True
    page_target: int | None = Field(default=None, ge=1, le=4)
    compile_timeout_s: int = Field(default=90, ge=10, le=600)
    min_description_chars: int = Field(default=400, ge=50, le=20_000)
    template: dict[str, Any] | None = None
    reword_skills: bool = True


class JobCleanupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto: bool = True
    min_score: float = Field(default=40, ge=0, le=100)
    sde_backend_only: bool = True


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apply_mode: Literal["dry_run", "auto"] = "dry_run"
    min_score: float = Field(default=70, ge=0, le=100)
    allowed_eligibility: list[Literal["eligible", "likely_eligible", "unknown"]] = ["eligible", "likely_eligible"]
    job_families: list[str] = Field(default_factory=list, max_length=20)
    max_job_age_days: int = Field(default=30, ge=1, le=365)
    max_per_cycle: int = Field(default=5, ge=1, le=25)
    daily_limit: int = Field(default=10, ge=0, le=40)
    email_applications: bool = True
    form_submissions: bool = True
    allowed_platforms: list[Literal["greenhouse", "lever", "ashby"]] = ["greenhouse", "lever", "ashby"]
    use_answer_bank: bool = True
    hold_dream_companies: bool = True
    fallback_to_master_resume: bool = True
    search_every_hours: float = Field(default=6, ge=1, le=168)
    add_feed_sources: bool = True
    discover_boards: bool = True
    max_board_checks: int = Field(default=10, ge=0, le=50)
    job_alert_emails: bool = True
    digest: bool = True


class TrackerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dream_companies: list[str] = Field(default_factory=list, max_length=40)
    cooldown_days: dict[str, int] = {}
    ask_daily: int = Field(default=5, ge=0, le=20)
    ask_again_days: int = Field(default=3, ge=1, le=60)
    auto_outcomes: bool = True

    @model_validator(mode="after")
    def _cooldowns(self):
        for name, days in self.cooldown_days.items():
            if not name.strip() or not 1 <= days <= 730:
                raise ValueError("cooldown_days: a company name and 1-730 days")
        return self


class ApolloSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_enrichments_per_day: int = Field(default=0, ge=0, le=25)
    recruiter_titles: list[str] = Field(default_factory=list, max_length=12)
    country: str | None = Field(default=None, max_length=60)


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    kinds: dict[NOTIFY_KINDS, bool] = {}
    relevant_min_score: float | None = Field(default=None, ge=0, le=100)
    high_match_min_score: float | None = Field(default=None, ge=0, le=100)
    daily_summary_hour: int = Field(default=20, ge=0, le=23)
    max_items_per_message: int = Field(default=10, ge=1, le=30)
    max_attempts: int = Field(default=5, ge=1, le=20)


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_backups: int = Field(default=14, ge=1, le=365)
    tasks: dict[Literal["collect_jobs", "followup_check", "maintenance", "backup", "gmail_sync", "rematch", "reminders",
                        "health_check", "notify", "outreach", "agents"], ScheduledTask] = {}
    deadline_warning_days: int = Field(default=3, ge=1, le=30)
    stale_application_days: int = Field(default=21, ge=3, le=120)

    @model_validator(mode="after")
    def _intervals(self):
        # Only the local notification pass may run more often than hourly; anything that contacts job sites,
        # Gmail or does heavy work keeps at least an hour between runs.
        for name, t in self.tasks.items():
            if name != "notify" and t.every_hours < 1:
                raise ValueError(f"{name}: every_hours must be at least 1")
        return self


class RoleCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    title_keywords: list[str]
    skill_keywords: list[str] = []
    title_required: bool = False  # True: description skills alone never place a job in this category


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
    "notifications": NotificationSettings.model_validate,
    "apollo": ApolloSettings.model_validate,
    "tracker": TrackerSettings.model_validate,
    "tailored_resumes": TailoredResumeSettings.model_validate,
    "agents": AgentSettings.model_validate,
    "job_cleanup": JobCleanupSettings.model_validate,
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


# Saved settings may still hold values of features that were removed (auto-apply, scheduled resume generation).
# They are ignored on read, so an old saved value never breaks validation or comes back to life.
REMOVED = {"tailored_resumes": ("max_per_run",)}
REMOVED_TASKS = ("autopilot", "tailor_resumes")
REMOVED_KINDS = ("submission_batch", "autopilot_exceptions", "tailored_resume", "tailored_failed")


def get(session: Session, key: str) -> Any:
    row = session.get(AppSetting, key)
    default = copy.deepcopy(DEFAULTS.get(key))
    if row is None:
        return default
    if isinstance(default, dict) and isinstance(row.value, dict):
        merged = default
        merged.update({k: v for k, v in row.value.items() if k not in REMOVED.get(key, ())})
        if key == "notifications":
            merged["kinds"] = {**copy.deepcopy(DEFAULTS[key]["kinds"]),
                               **{k: v for k, v in (row.value.get("kinds") or {}).items() if k not in REMOVED_KINDS}}
        if key == "scheduler":
            # Tasks added in later versions appear (disabled) even when you saved scheduler settings before.
            merged["tasks"] = {**copy.deepcopy(DEFAULTS[key]["tasks"]),
                               **{k: v for k, v in (row.value.get("tasks") or {}).items() if k not in REMOVED_TASKS}}
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
