"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from jobpilot.models.applications import (
    Application,
    ApplicationEvent,
    ApplicationEvidence,
    BrowserRun,
    ReviewItem,
)
from jobpilot.models.base import Base, utcnow
from jobpilot.models.interviews import Interview, LearningItem, PrepPack
from jobpilot.models.jobs import Company, Job, JobSighting, JobSource
from jobpilot.models.outreach import Contact, ContactInteraction, DoNotContact, EmailDraft, EmailTemplate
from jobpilot.models.profile import (
    CandidateProfile,
    Education,
    Employment,
    ProfileLink,
    Project,
    Skill,
)
from jobpilot.models.resume import Resume, ResumeVersion
from jobpilot.models.system import AppSetting, AuditLog, TaskRun

__all__ = [
    "Base", "utcnow", "AppSetting", "AuditLog", "TaskRun",
    "CandidateProfile", "Education", "Employment", "ProfileLink", "Project", "Skill",
    "Resume", "ResumeVersion",
    "Company", "Job", "JobSighting", "JobSource",
    "Contact", "ContactInteraction", "DoNotContact", "EmailDraft", "EmailTemplate",
    "Application", "ApplicationEvent", "ApplicationEvidence", "BrowserRun", "ReviewItem",
    "Interview", "LearningItem", "PrepPack",
]
