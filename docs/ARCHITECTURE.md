# JobPilot architecture

A single-process **modular monolith**: a FastAPI app that serves server-rendered
pages (Jinja2) and a CLI, both sharing the same service layer and a local
SQLite database. No runtime LLM, no paid services; every external integration
is optional and disabled by default.

```
            ┌──────────────┐      ┌──────────────┐
 browser ──▶│  web/routes  │      │    cli.py    │◀── cron / systemd timer
 (localhost)└──────┬───────┘      └──────┬───────┘
                   └──────────┬──────────┘
                       ┌──────▼───────┐
                       │   services   │  profile, resumes, jobs, matching,
                       │ (domain/use  │  contacts, drafts, applications,
                       │    cases)    │  review, interviews, skills,
                       └──┬───┬───┬───┘  analytics, backup, scheduler, tasks
             ┌────────────┘   │   └─────────────┐
       ┌─────▼─────┐   ┌──────▼──────┐   ┌──────▼───────┐
       │  sources  │   │   models    │   │ integrations │
       │ greenhouse│   │ SQLAlchemy  │   │ gmail (OAuth)│
       │ lever,    │   │ + Alembic   │   │ automation/  │
       │ json-ld,  │   │  (SQLite)   │   │ playwright   │
       │ imports   │   └─────────────┘   └──────────────┘
       └───────────┘
```

## Layers

| Package | Responsibility |
|---|---|
| `config.py` | Environment settings (`JOBPILOT_*`), derived paths, bind-safety check |
| `db.py` | Engine (SQLite WAL + foreign keys), sessions, Alembic upgrade with pre-migration backup |
| `models/` | ORM tables grouped by domain; one `Base.metadata`; migrations in `migrations/versions` |
| `services/` | All business rules. Functions take an explicit `Session`; routes and CLI are thin |
| `sources/` | Job-source adapters behind a common `JobSource` interface; polite HTTP client (timeouts, bounded retries, per-host rate limit, robots.txt) |
| `integrations/` | Optional Gmail client (manual OAuth installed-app flow over `requests`) |
| `automation/` | Optional Playwright form-assist; never submits; pauses on CAPTCHA/MFA/unknown fields |
| `web/` | FastAPI routes, Jinja2 templates, static CSS/JS; CSRF (same-origin) and trusted-host middleware |

## Key design decisions

- **Server-rendered UI** instead of a SPA: no build step, low memory, works offline.
- **SQLite + Alembic (batch mode)**: single file, easy backup; every schema change is a
  migration, and `migrate()` takes a backup before upgrading a non-empty database.
- **Facts have status**: profile facts and skills carry `confirmed` / `learning` /
  `unverified`. Only confirmed facts feed eligibility, resume selection, drafts and autofill.
- **Two scores**: *relevance* (does the job fit what I want?) and *eligibility*
  (do I meet the stated requirements?). Unknown requirements produce `unknown`
  eligibility, never `eligible`. Each score stores its explanation.
- **Deterministic templates** for outreach: missing facts render as visible
  `[[MISSING: …]]` placeholders and block approval; nothing is invented.
- **Human-in-the-loop gates**: resume content changes need approval; drafts need
  approval before Gmail send; browser automation never clicks submit; an application
  becomes `applied` only with confirmation evidence.
- **Tasks** run through `services/tasks.run_task`: `fcntl` lock per task (no overlap),
  bounded retries, and a `task_runs` record. The scheduler only invokes due tasks.
- **Audit log** for every consequential action (`audit_log` table + JSON log).
- **Information hub = provenance, not a copy**: `services/resume_parser.py` turns a resume into structured entries
  (deterministic; `pdftotext` when available, pypdf otherwise) and `services/hub.py` maps them onto the existing
  profile tables through `services/profile.py`. `profile_claims` records each value's source file hash, location,
  original wording and the import's outcome (created / updated / unchanged / conflict / suggestion); claim keys are
  name-based, so re-imports are idempotent and later resume versions can refresh values an import wrote while
  keeping your edits. Confirmed facts are never written by an import. `profile_imports` keeps each run's report.

## Data locations (all under `JOBPILOT_DATA_DIR`, default `./data`, git-ignored)

`jobpilot.db` · `files/` (resumes, evidence) · `backups/` · `logs/jobpilot.log` ·
`secrets/` (OAuth token, 0700) · `browser_profile/` · `locks/`

## Build order deviations (dependency-driven)

- **Backup/restore** moved from Stage 6 to Stage 1: migrations must back up existing data first.
- **Job explorer UI** built in Stage 2 (spec: Stage 3): imports and source runs needed somewhere to show results.
- **`email_drafts.application_id`** added in migration 0004 (Stage 4), after the `applications` table existed;
  `services/registry.py` lets outreach code reference applications/interviews without import cycles.

## Migrations

| Revision | Content |
|---|---|
| 0001 | audit log, task runs, app settings |
| 0002 | profile & facts, resumes & versions, companies, sources, jobs, sightings |
| 0003 | contacts, interactions, do-not-contact, email templates, drafts |
| 0004 | applications, events, evidence, review items, browser runs; `email_drafts.application_id` |
| 0005 | interviews, prep packs, learning backlog |
| 0006 | pipeline, sources, matching, preparation |
| 0007 | certifications, achievements, project kinds, role types, preferences |
| 0008 | information hub: `profile_imports`, `profile_claims` |
