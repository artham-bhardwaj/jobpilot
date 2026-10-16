"""Interview tracking, invitation extraction (suggestions only) and preparation packs.

Interviews are created by you. Text from an invitation e-mail (pasted, or read
from Gmail when authorised) only produces a *suggestion* in the review queue;
nothing is added to your schedule until you confirm it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Application, Employment, Interview, PrepPack, Project, utcnow
from jobpilot.models.interviews import INTERVIEW_FORMATS, INTERVIEW_STAGES
from jobpilot.services import applications as app_svc
from jobpilot.services import audit, profile as profile_svc, review


class InterviewIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    application_id: int
    round_no: int | None = Field(default=None, ge=1, le=20)
    round_name: str | None = Field(default=None, max_length=200)
    stage: Literal[INTERVIEW_STAGES] = "other"  # type: ignore[valid-type]
    scheduled_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=50)
    duration_minutes: int | None = Field(default=None, ge=5, le=600)
    format: Literal[INTERVIEW_FORMATS] = "video"  # type: ignore[valid-type]
    location_or_link: str | None = Field(default=None, max_length=1000)
    interviewers: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=10000)

    @field_validator("location_or_link")
    @classmethod
    def _safe_link(cls, v: str | None) -> str | None:
        if v and re.match(r"^\s*(javascript|data|vbscript):", v, re.I):
            raise ValueError("unsupported link scheme")
        return v


class OutcomeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    status: Literal["scheduled", "completed", "cancelled", "rescheduled", "no_show"]
    outcome: Literal["pending", "passed", "failed", "unknown"] = "pending"
    feedback: str | None = Field(default=None, max_length=10000)
    follow_up_actions: str | None = Field(default=None, max_length=5000)
    follow_up_done: bool = False


def save(session: Session, data: InterviewIn, interview_id: int | None = None, source: str = "manual",
         source_ref: str | None = None) -> Interview:
    app = session.get(Application, data.application_id)
    if app is None:
        raise ValueError("Application not found")
    if interview_id is None:
        iv = Interview(source=source, source_ref=source_ref)
        session.add(iv)
    else:
        iv = session.get(Interview, interview_id)
        if iv is None:
            raise LookupError("Interview not found")
    for k, v in data.model_dump().items():
        setattr(iv, k, v)
    if iv.round_no is None:
        existing = session.scalars(select(Interview.round_no).where(Interview.application_id == app.id)).all()
        iv.round_no = max([r for r in existing if r] or [0]) + 1
    session.flush()
    # An interview implies the application progressed — only if already submitted.
    if interview_id is None and app.status in ("applied", "assessment"):
        app_svc.transition(session, app, "interview" if data.stage != "online_assessment" else "assessment",
                           f"interview round {iv.round_no} recorded")
    audit.record(session, "interview.saved", "interview", iv.id, source=source)
    return iv


def record_outcome(session: Session, iv: Interview, data: OutcomeIn) -> Interview:
    for k, v in data.model_dump().items():
        setattr(iv, k, v)
    audit.record(session, "interview.outcome", "interview", iv.id, status=iv.status, outcome=iv.outcome)
    return iv


def upcoming(session: Session, limit: int = 50) -> list[Interview]:
    return list(session.scalars(select(Interview).where(Interview.status.in_(("scheduled", "rescheduled")))
                                .order_by(Interview.scheduled_at.asc().nulls_last()).limit(limit)))


def past(session: Session, limit: int = 100) -> list[Interview]:
    return list(session.scalars(select(Interview).where(Interview.status.not_in(("scheduled", "rescheduled")))
                                .order_by(Interview.scheduled_at.desc().nulls_last()).limit(limit)))


# ---- Invitation extraction (suggestions only) ---------------------------------

_LINK = re.compile(r"https?://[^\s<>\"')]+", re.I)
_MEETING_HOSTS = re.compile(r"zoom\.us|meet\.google\.com|teams\.microsoft\.com|teams\.live\.com|webex\.com|"
                            r"chime\.aws|whereby\.com|bluejeans\.com", re.I)
_ASSESS_HOSTS = re.compile(r"hackerrank\.com|codility\.com|codesignal\.com|hackerearth\.com|testgorilla\.com|"
                           r"mettl\.com|coderpad\.io|karat\.com", re.I)
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_PATTERNS = [
    # Tuesday, October 21, 2026 at 3:00 PM
    re.compile(rf"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?(?P<mon>(?:{_MONTHS})[a-z]*)\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>\d{{4}})"
               rf"(?:,?\s*(?:at|@|from)?\s*(?P<time>\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?))?", re.I),
    # 21 October 2026, 15:00
    re.compile(rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>(?:{_MONTHS})[a-z]*)\.?,?\s+(?P<year>\d{{4}})"
               rf"(?:,?\s*(?:at|@|from)?\s*(?P<time>\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?))?", re.I),
    # 2026-10-21 15:00
    re.compile(r"(?P<iso>\d{4}-\d{2}-\d{2})(?:[ T](?P<time>\d{1,2}:\d{2}))?"),
]
_TZ = re.compile(r"\b(IST|UTC|GMT|PST|PDT|EST|EDT|CST|CDT|CET|CEST|BST|SGT|AEST|[+-]\d{2}:?\d{2})\b")
# Most specific first. "hr"/"recruiter" come last: invitations are often *sent by* HR for technical rounds.
_STAGE_HINTS = [
    ("online_assessment", re.compile(r"online assessment|coding test|coding challenge|take[- ]home|assessment link", re.I)),
    ("system_design", re.compile(r"system design|architecture round|\bhld\b", re.I)),
    ("low_level_design", re.compile(r"low[- ]level design|\blld\b|machine coding|object[- ]oriented design", re.I)),
    ("coding", re.compile(r"\bcoding\b|data structures|algorithms|\bdsa\b", re.I)),
    ("technical", re.compile(r"technical", re.I)),
    ("hiring_manager", re.compile(r"hiring manager", re.I)),
    ("behavioral", re.compile(r"behaviou?ral", re.I)),
    ("recruiter_screen", re.compile(r"recruiter (?:call|screen)|phone screen|introductory call|screening call", re.I)),
    ("hr", re.compile(r"\bhr (?:round|interview|discussion)|human resources|culture fit", re.I)),
]
_ADDRESSES = re.compile(r"\S+@\S+|https?://\S+")
_INVITE = re.compile(r"interview|assessment|schedule|invit|round|meeting|call with", re.I)


def _parse_time(t: str | None) -> tuple[int, int]:
    if not t:
        return 0, 0
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t.strip(), re.I)
    if not m:
        return 0, 0
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return (h, mi) if h < 24 and mi < 60 else (0, 0)


def extract_invitation(text: str) -> dict[str, Any] | None:
    """Return a suggestion dict, or None if the text doesn't look like an invitation."""
    if not text or not _INVITE.search(text):
        return None
    when, when_text, has_time = None, None, False
    for rx in _DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            if m.groupdict().get("iso"):
                d = datetime.strptime(m.group("iso"), "%Y-%m-%d")
            else:
                mon = m.group("mon")[:3].lower()
                d = datetime.strptime(f"{m.group('day')} {mon} {m.group('year')}", "%d %b %Y")
            h, mi = _parse_time(m.group("time"))
            has_time = bool(m.group("time"))
            when, when_text = d.replace(hour=h, minute=mi), m.group(0).strip()
            break
        except ValueError:
            continue
    links = _LINK.findall(text)
    meeting = next((l for l in links if _MEETING_HOSTS.search(l)), None)
    assessment = next((l for l in links if _ASSESS_HOSTS.search(l)), None)
    hint_text = _ADDRESSES.sub(" ", text)  # e-mail addresses / links (hr@…, …/system-design) are not stage hints
    stage = "online_assessment" if assessment else next((s for s, rx in _STAGE_HINTS if rx.search(hint_text)), "other")
    tz = _TZ.search(text)
    fmt = "online_test" if assessment else ("video" if meeting else ("phone" if re.search(r"\bphone\b|\bcall you\b", text, re.I) else "other"))
    if not (when or meeting or assessment):
        return None
    return {"scheduled_at": when.isoformat() if when else None, "time_known": has_time, "date_text": when_text,
            "timezone": tz.group(1) if tz else None, "stage": stage, "format": fmt,
            "location_or_link": meeting or assessment, "excerpt": text.strip()[:1500]}


def suggest_from_text(session: Session, app: Application, text: str, source_ref: str | None = None) -> dict | None:
    s = extract_invitation(text)
    if s is None:
        return None
    s["source_ref"] = source_ref
    review.open_item(session, "interview_detected", "application", app.id,
                     f"Possible interview: {app.job.title} at {app.job.company_name or '?'}",
                     f"{s['stage'].replace('_', ' ')} · {s['date_text'] or 'date not found'}"
                     + (f" · {s['location_or_link']}" if s["location_or_link"] else ""), data=s)
    audit.record(session, "interview.suggested", "application", app.id, source_ref=source_ref)
    return s


# ---- Preparation packs -----------------------------------------------------------

TOPICS: dict[str, dict[str, list[str]]] = {
    "dsa": {"title": ["Data structures & algorithms"], "items": [
        "Complexity analysis (time/space, amortised)", "Arrays & strings: two pointers, sliding window, prefix sums",
        "Hashing: maps/sets, frequency counting", "Stacks & queues, monotonic stack", "Linked lists: reversal, cycle detection",
        "Binary search (including on the answer)", "Trees: traversals, BST operations, LCA", "Heaps / priority queues: top-k, merge k lists",
        "Graphs: BFS/DFS, topological sort, shortest paths, union-find", "Recursion & backtracking",
        "Dynamic programming: 1D/2D, knapsack, LIS, intervals"],
        "practice": ["Two Sum / variants", "Longest substring without repeating characters", "Merge intervals",
                     "Kth largest element", "Number of islands", "Course schedule (topological sort)", "Coin change"]},
    "java": {"title": ["Java"], "items": [
        "OOP principles, interfaces vs abstract classes", "Collections: HashMap internals, ArrayList vs LinkedList, ConcurrentHashMap",
        "equals/hashCode contract, immutability", "Exceptions: checked vs unchecked, try-with-resources",
        "Generics and type erasure", "Java 8+: streams, lambdas, Optional, records", "Concurrency: threads, ExecutorService, synchronized, volatile, CompletableFuture",
        "JVM memory model, heap/stack, garbage collection basics"],
        "practice": ["Explain how HashMap handles collisions", "Implement a thread-safe singleton", "When would you use a ReentrantLock?"]},
    "spring boot": {"title": ["Spring Boot"], "items": [
        "Dependency injection / IoC container, bean scopes and lifecycle", "Auto-configuration and starters",
        "REST controllers, validation, global exception handling", "Spring Data JPA / Hibernate: entities, relationships, N+1 problem, fetch types",
        "@Transactional: propagation and isolation", "Configuration properties and profiles",
        "Spring Security basics (authentication vs authorisation, JWT)", "Testing: @SpringBootTest, @WebMvcTest, Mockito", "Actuator and observability"],
        "practice": ["Walk through what happens when a request hits a @RestController", "How do you avoid N+1 queries?"]},
    "sql": {"title": ["SQL & databases"], "items": [
        "Joins (inner/left/self), GROUP BY / HAVING", "Window functions (ROW_NUMBER, RANK, running totals)", "Subqueries vs CTEs",
        "Indexes: B-tree, composite indexes, when indexes aren't used", "Normalisation and denormalisation trade-offs",
        "Transactions, ACID, isolation levels, locking", "Reading query plans (EXPLAIN)"],
        "practice": ["Second-highest salary per department", "Find duplicate rows", "Users active on consecutive days"]},
    "python": {"title": ["Python"], "items": [
        "Data model: mutability, references, copying", "Generators, iterators, comprehensions", "Decorators and context managers",
        "The GIL, threading vs multiprocessing vs asyncio", "Typing and dataclasses / pydantic", "Testing with pytest"],
        "practice": ["Write a decorator that retries a function", "Explain when asyncio helps"]},
    "rest": {"title": ["REST APIs & backend engineering"], "items": [
        "HTTP methods, safety and idempotency (GET/PUT/DELETE vs POST, PATCH)", "Status codes: 2xx/4xx/5xx and when to use each",
        "Resource and URL design, versioning", "Pagination, filtering and sorting", "Validation and consistent error responses",
        "Authentication vs authorisation; sessions, JWT and OAuth basics", "Idempotency keys, retries and timeouts",
        "Caching headers and ETags", "Rate limiting", "API documentation (OpenAPI) and backward compatibility"],
        "practice": ["Design the API for a library-management service", "How do you make a payment endpoint safe to retry?"]},
    "testing": {"title": ["Testing"], "items": [
        "Unit vs integration vs end-to-end tests; the test pyramid", "Mocks, stubs and fakes — and when not to mock",
        "Integration testing of APIs and databases; test data management", "Contract testing between services",
        "Testing in CI; flaky tests and how to fix them", "Writing a good defect report"],
        "practice": ["How would you test a REST endpoint that writes to a database?", "How do you find the cause of a flaky test?"]},
    "lld": {"title": ["Low-level design"], "items": [
        "SOLID principles", "Design patterns: strategy, factory, observer, builder, singleton, decorator",
        "Class diagrams and responsibilities", "Concurrency in designs (locks, thread safety)"],
        "practice": ["Parking lot", "LRU cache", "Rate limiter", "Elevator system", "Expense-sharing app"]},
    "hld": {"title": ["High-level / system design"], "items": [
        "Requirements & estimation (QPS, storage)", "Load balancing, horizontal scaling, statelessness",
        "Caching strategies and invalidation", "Database replication, sharding, SQL vs NoSQL",
        "CAP theorem and consistency models", "Message queues / Kafka, async processing",
        "Rate limiting, idempotency, retries", "Observability: logs, metrics, tracing"],
        "practice": ["URL shortener", "News feed", "Chat system", "Notification service"]},
    "ml": {"title": ["Machine learning"], "items": [
        "Bias/variance, overfitting, regularisation", "Evaluation metrics: precision/recall, ROC-AUC, F1, RMSE",
        "Train/validation/test splits, cross-validation, leakage", "Feature engineering and preprocessing",
        "Tree ensembles vs neural networks", "Transformers & embeddings basics", "Model deployment and monitoring"],
        "practice": ["How would you handle class imbalance?", "Explain attention in transformers at a high level"]},
    "behavioral": {"title": ["Behavioural"], "items": [
        "STAR structure (Situation, Task, Action, Result)", "Ownership and impact", "Handling conflict or disagreement",
        "A failure and what you learned", "Working under ambiguity / tight deadlines", "Why this company and role"],
        "practice": ["Tell me about yourself (2 minutes)", "Describe a technical challenge you solved", "Tell me about a mistake you made"]},
}
STAGE_TOPICS = {
    "recruiter_screen": ["behavioral"],
    "hr": ["behavioral"],
    "behavioral": ["behavioral"],
    "hiring_manager": ["behavioral", "hld"],
    "online_assessment": ["dsa", "sql"],
    "coding": ["dsa"],
    "technical": ["dsa", "{lang}", "{framework}", "sql", "{api}"],
    "system_design": ["hld", "lld"],
    "low_level_design": ["lld", "{lang}"],
    "onsite": ["dsa", "{lang}", "{framework}", "sql", "{api}", "lld", "hld", "behavioral"],
    "other": ["dsa", "{lang}", "{api}", "behavioral"],
}


def _lang_framework(skills: set[str], category: str | None) -> tuple[str | None, str | None]:
    if "java" in skills or category == "java_backend":
        return "java", "spring boot"
    if "python" in skills or category in ("python_backend", "ai_ml"):
        return "python", ("ml" if category == "ai_ml" or "machine learning" in skills else None)
    return None, None


def generate_prep_pack(session: Session, iv: Interview) -> PrepPack:
    app, job = iv.application, iv.application.job
    details = job.match_details or {}
    skills = details.get("skills") or {}
    job_skills = set((skills.get("mandatory") or []) + (skills.get("preferred") or []))
    lang, framework = _lang_framework(job_skills, job.category)
    keys: list[str] = []
    backend = job.category in ("java_backend", "python_backend") or bool(
        job_skills & {"rest api", "microservices", "spring boot", "django", "fastapi", "flask"})
    for k in STAGE_TOPICS.get(iv.stage, STAGE_TOPICS["other"]):
        k = k.replace("{lang}", lang or "").replace("{framework}", framework or "").replace("{api}", "rest" if backend else "")
        if k and k in TOPICS and k not in keys:
            keys.append(k)
    if job_skills & {"unit testing", "junit", "pytest", "selenium"} and "testing" not in keys and iv.stage != "hr":
        keys.append("testing")
    if job.category == "ai_ml" and "ml" not in keys and iv.stage in ("technical", "onsite", "other"):
        keys.append("ml")

    confirmed = profile_svc.confirmed_skills(session)
    learning = profile_svc.learning_skills(session)
    # STAR prompts only from confirmed facts, each labelled with what kind of experience it is, so a personal
    # project is never told as production work and testing work is never told as development.
    stories = []
    for e in session.scalars(select(Employment).where(Employment.status == "confirmed")):
        kind = (e.role_type or "").replace("_", " ")
        if e.role_type and e.role_type != profile_svc.DEVELOPMENT_ROLE:
            stories.append(f"Work — {e.title} at {e.company} ({kind}): draw on what you actually did there; present it "
                           f"as {kind} work, not as software development")
        else:
            stories.append(f"Work — {e.title} at {e.company}" + (f" ({kind})" if kind else
                           " (kind of work not recorded on your profile: describe it as it was)"))
    project_questions = []
    for p in session.scalars(select(Project).where(Project.status == "confirmed")):
        label = {"academic": "academic project", "learning_exercise": "learning exercise",
                 "open_source": "open-source contribution"}.get(p.kind or "personal", "personal project")
        stories.append(f"{label.capitalize()} — {p.name}: not production experience; say so if asked")
        tech = ", ".join(p.technologies or [])
        project_questions += [f"{p.name}: walk me through the problem, your design and what you would change",
                              f"{p.name}: why {tech}? What were the alternatives?" if tech else
                              f"{p.name}: why did you choose the technologies you used?",
                              f"{p.name}: how did you test it, and what broke?"]
    sections = [{"title": TOPICS[k]["title"][0], "items": TOPICS[k]["items"], "practice": TOPICS[k]["practice"]} for k in keys]
    content = {
        "header": {"job": job.title, "company": job.company_name, "stage": iv.stage, "round": iv.round_name or iv.round_no,
                   "when": iv.scheduled_at.isoformat() if iv.scheduled_at else None,
                   "resume": app.resume_version.label if app.resume_version else None},
        "job_skills_you_have": sorted(job_skills & confirmed),
        "job_skills_to_review": sorted(job_skills - confirmed),
        "job_skills_learning": sorted(job_skills & learning),
        "job_skills_missing": sorted(job_skills - confirmed - learning),
        "sections": sections,
        "stories": [f"Prepare a STAR story drawing on: {s}" for s in stories] or
                   ["Add confirmed employment or projects to your profile to get story prompts."],
        "project_questions": project_questions[:15],
        "questions_to_ask": ["What does success look like in the first 90 days?", "How is the team structured and how does work get prioritised?",
                             "What does the development and release process look like?", "What are the next steps in the process?"],
        "notes": ["Topics are general preparation for this stage and the job's stated skills. JobPilot has no "
                  "company-specific knowledge of this interview.",
                  "Review the job description and the exact resume version you submitted."],
    }
    pack = PrepPack(interview_id=iv.id, content=content,
                    inputs={"job_id": job.id, "resume_version_id": app.resume_version_id, "stage": iv.stage})
    session.add(pack)
    session.flush()
    audit.record(session, "interview.prep_generated", "interview", iv.id)
    return pack
