"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from jobpilot.models.applications import (
    AnswerBankEntry,
    Application,
    ApplicationEvent,
    ApplicationQuestion,
    ApplicationEvidence,
    BrowserRun,
    ReviewItem,
)
from jobpilot.models.autopilot import SubmissionBatch
from jobpilot.models.base import Base, utcnow
from jobpilot.models.hub import ProfileClaim, ProfileImport
from jobpilot.models.interviews import Interview, LearningItem, PrepPack
from jobpilot.models.notifications import IntegrationState, Notification, NotificationKey, TelegramUpdate
from jobpilot.models.jobs import Company, Job, JobSighting, JobSource, MatchFeedback, SourceRun
from jobpilot.models.outreach import (
    Contact,
    ContactInteraction,
    ContactJob,
    DoNotContact,
    DraftRevision,
    EmailDraft,
    EmailTemplate,
)
from jobpilot.models.profile import (
    Achievement,
    CandidateProfile,
    Certification,
    Education,
    Employment,
    ProfileLink,
    Project,
    Skill,
)
from jobpilot.models.resume import Resume, ResumeVersion
from jobpilot.models.system import AppSetting, AuditLog, TaskRun
from jobpilot.models.tailored import TailoredResume

__all__ = [
    "TailoredResume",
    "Base", "utcnow", "AppSetting", "AuditLog", "TaskRun",
    "CandidateProfile", "Education", "Employment", "ProfileLink", "Project", "Skill", "Certification", "Achievement",
    "Resume", "ResumeVersion", "ProfileClaim", "ProfileImport", "Notification", "NotificationKey", "SubmissionBatch", "TelegramUpdate", "IntegrationState",
    "Company", "Job", "JobSighting", "JobSource",
    "Contact", "ContactInteraction", "DoNotContact", "EmailDraft", "EmailTemplate",
    "Application", "ApplicationEvent", "ApplicationEvidence", "BrowserRun", "ReviewItem",
    "Interview", "LearningItem", "PrepPack",
    "SourceRun", "MatchFeedback", "AnswerBankEntry", "ApplicationQuestion", "DraftRevision", "ContactJob",
]
