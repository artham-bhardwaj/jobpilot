# JobPilot

A local-first job search and application manager. It collects jobs from sources you configure,
scores them against your **verified** profile, picks the right approved resume, helps you prepare
applications and recruiter outreach, tracks interviews, and reports results.

- Runs entirely on your machine (FastAPI + SQLite). No paid services, no AI APIs, no API keys.
- Never invents facts: only information you confirm is used in scores, resumes, emails and forms.
- Never submits applications or sends email on its own. Every consequential step waits for you.

> JobPilot only sees jobs from the sources you add (company boards, career pages, imports).
> It does not and cannot cover the whole internet.

## Requirements

- Ubuntu (or any Linux/macOS), Python 3.11+ (tested on 3.12.3)
- Optional: Google Chrome (for resume PDF generation and browser-assisted applications)
- ~150 MB RAM for the server; a browser session adds ~300–500 MB while open

## Setup

```bash
cd ~/projects/jobpilot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,browser]"     # drop ",browser" to skip Playwright
cp .env.example .env                          # optional; all settings have safe defaults
.venv/bin/jobpilot init                       # creates ./data (owner-only, 0700) and the database
.venv/bin/jobpilot serve                      # http://127.0.0.1:8765
```

`init` is required once: other commands (including `serve`) refuse to create a database
implicitly, so running from the wrong directory gives a clear error instead of an empty new
database. Paths: `.env` is read from the current directory (or from `JOBPILOT_ENV_FILE`), and a
relative `JOBPILOT_DATA_DIR` resolves against the current directory. `init` and `serve` print the
resolved locations.

Playwright uses your installed Chrome (`JOBPILOT_BROWSER_CHANNEL=chrome`), so no browser download
is needed. Without Chrome, run `.venv/bin/playwright install chromium` and set the channel to empty.

## First steps

1. **Information hub** → import your resume (or `jobpilot hub import --file resume.pdf`), resolve conflicts, and
   confirm what is accurate. Then **Profile** → fill in anything else, then *Confirm details*. Add skills and mark the ones
   you can genuinely claim as *confirmed*. For each role, set **Kind of work** (only "software development"
   counts as development experience).
2. **Resumes** → *Create standard resume slots* (Master, Java Backend, SDE I, General SWE, Python
   Backend, AI/ML) → import your existing PDF/DOCX into each → review → *Approve*.
3. **Settings** → adjust `search_preferences` (keywords, exclusions, locations, levels, salary) and
   `matching` thresholds.
4. **Sources** → add company boards, e.g. Greenhouse `{"board_token": "<company>"}` or Lever
   `{"site": "<company>"}` → *Run*. Or paste a job URL / import a CSV under **Jobs → Add**.
5. Work from **Overview**, **Jobs**, and the **Review queue**.

See [docs/USER_GUIDE.md](docs/USER_GUIDE.md) for the full workflow.

## Importing your resume into the profile

```bash
.venv/bin/jobpilot hub import --file ~/Documents/resume.pdf --dry-run   # report only
.venv/bin/jobpilot hub import --file ~/Documents/resume.pdf             # backup first, then import
.venv/bin/jobpilot hub report                                           # latest import report
```

Or use **Information hub** (`/hub`) in the dashboard. The import fills employment, education, skills, projects,
certifications, achievements and links as **unverified resume claims** with their source (file hash, page, section,
bullet). Confirmed facts are never overwritten: differences are listed as conflicts for you to resolve. Re-importing
the same file is a no-op. See [docs/USER_GUIDE.md §1a](docs/USER_GUIDE.md).

## Collecting jobs manually

1. **Sources → Add source**, e.g. type `greenhouse`, config `{"board_token": "<company>"}` (from
   `boards.greenhouse.io/<company>`), or `lever` / `ashby` / `jsonld` (see the formats on that page).
2. Click **dry run** first. It fetches the board and shows what *would* be added, updated or expired without
   saving any jobs. It still contacts the site, so only use sources you're allowed to read.
3. Click **run**. Each run is recorded on the source's page (status, new/duplicate/missing/expired counts, 429s,
   errors). Outages, rate limits, malformed responses and empty or collapsed listings never expire jobs; a job
   expires only after consecutive successful runs no longer list it.

CLI equivalents: `jobpilot collect --dry-run --source <id>` then `jobpilot collect --source <id>`.
Other entry points: **Jobs → Add** (paste a job URL, upload CSV/JSON, or type a job in).

## Reviewing jobs

- **Review queue**: everything needing a decision, with the reason and the safest next action. Strong
  matches, eligibility uncertainties, missing resume variants, missing profile facts, contacts to verify, drafts
  to approve, paused browser sessions, failed operations. Bulk actions are limited to mark-reviewed / shortlist /
  dismiss / acknowledge.
- **Job page**: relevance (seven weighted components, each with evidence), eligibility (hard conflicts marked),
  data confidence, required/preferred/mentioned-only skills, and where every parsed fact came from.
- **Resume-selection review** (link on each job): which approved resume is chosen and why, against your
  verified facts.
- **Matching**: calibrate. Inspect score components across a sample, mark matches useful or irrelevant
  (recorded only), preview other weights side by side, and save them explicitly.
- **Start application** creates a persisted preparation record; add the form's questions, reuse approved
  answers from the **Answer bank** (sensitive questions you always answer yourself), approve each answer, then
  follow the checklist. "Applied" requires your confirmation evidence.

## Command line

```bash
jobpilot serve [--port N]                 # dashboard
jobpilot collect [--dry-run] [--source ID ...]  # run job sources (dry run saves nothing)
jobpilot add-url URL [--company NAME]     # add one job posting
jobpilot import-jobs FILE.csv|json        # import jobs
jobpilot rematch                          # re-score active jobs
jobpilot assist APP_ID --approve-host HOST # open browser for one approved site (never submits)
jobpilot backup create|list|restore --name FILE
jobpilot hub import --file F|--version ID [--dry-run]   # resume → profile (see below)
jobpilot hub report [--id N] | hub link-docs --project NAME --path DIR
jobpilot export jobs|applications|contacts|drafts|interviews|summary|all-json [--out FILE]
jobpilot delete-data profile|contacts|outreach|everything [--include-backups]
jobpilot scheduler run-due|status|run --task NAME|systemd [--out DIR]
jobpilot gmail auth|status|sync|revoke    # optional, see docs/GMAIL_SETUP.md
jobpilot db upgrade|current
jobpilot doctor                           # read-only diagnostics: paths, schema, server, health, integrity
jobpilot --debug <command>                # show a full traceback if something fails
```

`python -m jobpilot …` works the same as `jobpilot …`.

## Tests

```bash
.venv/bin/pytest            # 439 tests, offline (fake HTTP, fake DNS), synthetic fixtures only (~6 min)
```

A few tests drive your local Chrome headlessly (PDF rendering, form-fill safety); they are skipped
automatically if Chrome/Playwright is unavailable. Passing offline tests do **not** prove that live
job boards, Gmail or employer forms behave the same way. See `IMPLEMENTATION_STATUS.md`.

## Optional scheduling

Scheduled tasks (collect jobs, expiry check, score updates, deadline/application reminders, follow-up check,
backup, health check, Gmail reply sync) are **off** by default. Enable each in **Settings → scheduler**, then either:

- set `JOBPILOT_SCHEDULER_ENABLED=true` so `jobpilot serve` runs them while it is open, or
- install the systemd user timer (runs while you're logged in; `Persistent=true` catches up after
  the machine was off):

  ```bash
  .venv/bin/jobpilot scheduler systemd --out deploy/systemd
  mkdir -p ~/.config/systemd/user && cp deploy/systemd/jobpilot-scheduler.* ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now jobpilot-scheduler.timer
  ```

Overlapping runs are prevented with per-task file locks. Running both the in-process scheduler and the timer is
safe (the locks stop a task from running twice), but one of them is enough. Nothing runs while the laptop sleeps;
due tasks run on the next pass after it wakes, once each (reminders are raised once per fact, so catching up never
duplicates them). A failed task backs off (15 min, 30 min, 1 h, …) instead of retrying on every pass.
Scheduled tasks never send email or submit applications.

## Safe start and stop

```bash
cd ~/projects/jobpilot
.venv/bin/jobpilot db current        # read-only: shows {"current", "head", "pending"}; changes nothing
.venv/bin/jobpilot doctor            # optional, read-only: health, integrity, whether a server is already running
.venv/bin/jobpilot serve             # migrates if "pending" was true (backup first), then serves 127.0.0.1:8765
```

Every command except `db current` applies pending migrations, always taking a `pre-migrate-<rev>` backup first
(`jobpilot backup list` shows it).

Stop with **Ctrl-C** in that terminal (or `kill <pid>`, i.e. SIGTERM): the server finishes the current request,
waits for a running scheduled task, and exits. Database writes are transactional, so even an unclean stop
(power loss, `kill -9`) leaves the last committed state; a source run interrupted mid-way saves nothing, and a
task left "running" is recorded as failed ("Interrupted") the next time JobPilot starts. A browser-assistance
window can be closed at any time; the application record keeps what was saved before.

## Troubleshooting and recovery

| Symptom | What to do |
|---|---|
| `No JobPilot database at …` | You're in the wrong directory, or `JOBPILOT_DATA_DIR` points elsewhere. `cd ~/projects/jobpilot` and retry; `jobpilot doctor` prints the paths it uses. Run `jobpilot init` only for a brand-new install. |
| `Port 8765 … is already in use` on serve | A server is already running (`jobpilot doctor` says so). Use it, or stop it with Ctrl-C in its terminal, or start on another port: `jobpilot serve --port 8766`. |
| "migration pending" in `db current` / `doctor` | Normal after an update. `jobpilot serve` (or `jobpilot db upgrade`) applies it after taking a `pre-migrate-<rev>` backup. |
| A failed migration or bad data | Stop the server, then `jobpilot backup list` and `jobpilot backup restore --name <file>`; the current state is backed up first, and an older-schema backup is upgraded on restore. |
| A source keeps failing | See **Sources → (source) → history**. Failed, rate-limited or empty runs never change or expire existing jobs; the scheduler backs off. Disable the source if the board moved. |
| `doctor` / `/health` shows a failing check | The detail line says what's wrong (permissions, disk, schema, integrity). Integrity failure: restore the newest good backup. |
| Something odd | `jobpilot --debug <command>` shows the traceback; logs are in `data/logs/jobpilot.log` (System page shows them). |

Known limitations and what has and hasn't been verified against live services are listed in
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Configuration

All configuration is via environment variables (see [.env.example](.env.example)). Integrations
(browser automation, Gmail, in-process scheduler) are disabled until you enable them. Runtime
preferences (search, matching thresholds, role categories, outreach pacing, scheduler) are edited
under **Settings** and validated.

## Documentation

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — how to use each feature
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design overview
- [docs/SECURITY.md](docs/SECURITY.md) — security and privacy notes
- [docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md) — optional Gmail OAuth setup
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) — verified status, defects, limitations (source of truth)

## Data

Everything lives under `./data` (git-ignored, created with owner-only permissions): `jobpilot.db`, `files/` (resumes, evidence),
`backups/`, `logs/`, `secrets/` (OAuth token), `browser_profile/`. Back up with
`jobpilot backup create` or the System page; a backup is also taken automatically before every
schema migration and before restores/deletions.
