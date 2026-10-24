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

## Notifications and Telegram

`services/notifications.py` derives events from the database on each `notify` pass (jobs above thresholds, drafts to
review, application events, replies, due follow-ups, failed tasks and source runs), records each event key once in
`notification_keys`, groups routine events, and stores messages in `notifications`; delivery is a separate step that
marks a message sent only with Telegram's message id. `integrations/telegram.py` is the Bot API client (secret
token, redacted errors, retries/429 handling, 4096-char chunking). `services/telegram_bot.py` answers read-only
commands from the configured chat by long polling under the `telegram_poll` file lock; processed update ids and the
offset are persisted. The poller runs as a daemon thread in `jobpilot serve` (or `jobpilot telegram poll`).

## Tailored resumes

`services/tailored.py` parses the registered LaTeX source of the approved Master resume losslessly, maps every line to a
confirmed profile fact, re-orders lines by the job's skills (never rewording or adding), compiles locally (latexmk /
pdflatex, no shell escape, timeout, temporary directory), validates the PDF against the approved resume's text, and
stores it in `tailored_resumes` (migration 0012). `attachment_for` is the single place uploads choose a file: the job's
valid, current tailored resume, else the application's checksum-verified approved resume. Details:
[TAILORED_RESUMES.md](TAILORED_RESUMES.md).
Resumes are generated only on request (job page or CLI); there is no scheduled generation.

## Regions and applying

`services/regions.py` tags each job `india`, `remote` (remote and open to India / the UAE), `dubai` or `other` from its
location, work mode and title, stored in `jobs.regions` (migration 0013) and set on every ingest. The Jobs page filters
by it (region tabs) and the dashboard groups top matches by it. Board sources accept `"regions"` next to
`location_keywords`; `sources/remote.py` adds the Himalayas and Jobicy remote boards, and
`services/regional_sources.py` holds the recommended India / remote / Dubai source presets. JobPilot never submits
applications: the autopilot service, its scheduler task and Telegram batch approvals were removed on 2026-10-23
(the `submission_batches` table is kept for history).

## Data locations (all under `JOBPILOT_DATA_DIR`, default `./data`, git-ignored)

`jobpilot.db` · `files/` (resumes, evidence, `templates/` resume source, `tailored_resumes/`) · `backups/` · `logs/jobpilot.log` ·
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
| 0009 | Gmail synchronisation state on `email_drafts` |
| 0010 | notifications: `notifications`, `notification_keys`, `telegram_updates`, `integration_state` |
| 0011 | autopilot submission batches (historical: auto-apply was removed); `notifications.reply_markup` |
| 0012 | `tailored_resumes`: job-specific PDFs from the Master resume's LaTeX source |
| 0013 | `jobs.regions` (`,india,remote,dubai,` tags; existing jobs back-filled) |
