# JobPilot

> **Using it day to day? Start with [howtouse.md](howtouse.md).**

A local-first job search and application manager. It collects jobs from sources you configure,
scores them against your **verified** profile, picks the right approved resume, helps you prepare
applications and recruiter outreach, tracks interviews, and reports results.

- Runs entirely on your machine (FastAPI + SQLite). No paid services, no AI APIs, no API keys.
- Never invents facts: only information you confirm is used in scores, resumes, emails and forms.
- Four rule-based agents (Search, Resume, Apply, Manager; [docs/AGENTS.md](docs/AGENTS.md)) can apply for you: by
  email when a posting asks for it, and on Greenhouse / Lever / Ashby forms only when there is no CAPTCHA, bot check,
  login or legal question. They start in dry-run mode; everything they can't do safely waits for you.
- Optional: Gmail drafts/sending/reply detection ([docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md)) and Telegram
  notifications plus read-only commands ([docs/TELEGRAM.md](docs/TELEGRAM.md)), both free.
- Finds jobs in India, remote jobs open to India or the UAE, and jobs in Dubai / UAE, with region tabs on the Jobs page
  and a region overview on the dashboard ([howtouse.md](howtouse.md) §9).
- Tailored resumes for any job, made only when you ask on the job's page
  ([docs/TAILORED_RESUMES.md](docs/TAILORED_RESUMES.md)).
- Runs in Docker with one command, on your existing data: `./jobpilot-docker start`
  ([docs/DOCKER.md](docs/DOCKER.md)). Browser-assisted applying still needs the native install.

> JobPilot only sees jobs from the sources you add (company boards, career pages, imports).
> It does not and cannot cover the whole internet.

## Quick start with Docker

```bash
./jobpilot-docker switch-to-docker   # first time: stops the native service (asks first), backs up, starts Docker
./jobpilot-docker start              # afterwards; then open http://127.0.0.1:8765
./jobpilot-docker stop | logs -f | status | update | cli doctor
./jobpilot-docker switch-to-native   # go back to the native install
```

It uses this folder's `data/` and copies your `.env` settings into a private `.env.docker`. Details:
[docs/DOCKER.md](docs/DOCKER.md).

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
jobpilot gmail auth|status|check|sync|test-draft|create-draft|draft-status|inspect   # docs/GMAIL_SETUP.md
jobpilot telegram status|check|find-chat|set-chat ID|test|notify [--dry-run]|summary|poll  # docs/TELEGRAM.md
jobpilot db upgrade|current
jobpilot doctor                           # read-only diagnostics: paths, schema, server, health, integrity
jobpilot --debug <command>                # show a full traceback if something fails
```

`python -m jobpilot …` works the same as `jobpilot …`.

## Tests

```bash
.venv/bin/pytest            # 566 tests, offline (fake HTTP, fake DNS), synthetic fixtures only (~6 min)
```

A few tests drive your local Chrome headlessly (PDF rendering, form-fill safety); they are skipped
automatically if Chrome/Playwright is unavailable. Passing offline tests do **not** prove that live
job boards, Gmail or employer forms behave the same way. See `IMPLEMENTATION_STATUS.md`.

## Optional scheduling

Scheduled tasks (collect jobs, expiry check, score updates, deadline/application reminders, follow-up check,
backup, health check, Gmail reply sync, Telegram notifications) are **off** by default. The `notify` task may run
every 5 minutes or more; tasks that contact job sites or Gmail run at most hourly. Enable each in **Settings → scheduler**, then either:

- set `JOBPILOT_SCHEDULER_ENABLED=true` so `jobpilot serve` runs them while it is open, or
- install the systemd user timer (runs while you're logged in; each pass runs whatever is overdue, so missed
  work is caught up on the next pass after the machine wakes):

  ```bash
  .venv/bin/jobpilot scheduler systemd --out deploy/systemd
  mkdir -p ~/.config/systemd/user && cp deploy/systemd/jobpilot-scheduler.* ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now jobpilot-scheduler.timer
  ```

Check it with `jobpilot scheduler status` (or **System → Scheduled tasks**): whether a scheduler is running, the last
pass and what it ran (empty passes are recorded too), the expected next pass, and per task the last run (manual or
scheduled, success or failure), last success, next due time and the pass expected to run it. A due task runs at the
next pass (every `JOBPILOT_SCHEDULER_POLL_SECONDS`, default 300 s), not at the exact moment it becomes due. A scheduled
collection in which every source failed is recorded as **failed** (and retried with backoff), never as an empty
success. Backups are verified before they are kept, and retention never removes the last valid backup.

Overlapping runs are prevented with per-task file locks. Running both the in-process scheduler and the timer is
safe (the locks stop a task from running twice), but one of them is enough. Nothing runs while the laptop sleeps;
due tasks run on the next pass after it wakes, once each (reminders are raised once per fact, so catching up never
duplicates them). A failed task backs off (15 min, 30 min, 1 h, …) instead of retrying on every pass.
Scheduled tasks never send email or submit applications.

**Laptop off = paused.** JobPilot runs only on your laptop. While it is asleep, shut down, or JobPilot isn't running,
nothing is collected, checked or notified. When it runs again, each overdue task runs once (not once per missed
interval), and Telegram commands sent meanwhile are answered (Telegram keeps them for about a day).

## Job sources (what is actually monitored)

Only the sources you add on **Sources** are fetched, nothing else:

| Type | What it reads | Config |
|---|---|---|
| `greenhouse` | a company's public Greenhouse board API (`boards-api.greenhouse.io`) | `{"board_token": "<company>"}` |
| `lever` | a company's public Lever postings API | `{"site": "<company>"}` |
| `ashby` | a company's public Ashby job board API | `{"board": "<company>"}` (from `jobs.ashbyhq.com/<board>`) |
| `jsonld` | individual job pages that publish schema.org `JobPosting` data | `{"urls": ["https://…", …]}` (max 50) |
| `sitemap` | job pages listed in a career site's own sitemap (the one its robots.txt names), filtered by keywords in the URL, read for schema.org `JobPosting` data; works for Workday career sites | `{"sitemap_url": "https://<tenant>.wd5.myworkdayjobs.com/<site>/siteMap.xml", "title_keywords": [...], "location_keywords": [...], "exclude_keywords": [...], "max_pages": 25}` |
| manual | URLs you paste, CSV/JSON imports, typed-in jobs | Jobs → Add |

Configured target companies (checked 2026-10-17; see each source's note on **Sources**):

| Company | Official careers site uses | Source |
|---|---|---|
| Visa | Workday `visa.wd5.myworkdayjobs.com/Visa` | `sitemap` |
| Mastercard | Phenom site + Workday `mastercard.wd1.myworkdayjobs.com/CorporateCareers` | `sitemap` |
| Adobe | Phenom site + Workday `adobe.wd5.myworkdayjobs.com/external_experienced` | `sitemap` |
| Atlassian | iCIMS | not supported |
| JPMorgan Chase | Oracle Cloud HCM (script-rendered) | not supported |
| Google, Microsoft, Amazon | their own career systems | not supported |

Workday sitemaps list about 100 postings each (the newest), not every opening, so a sitemap source sees a recent
slice of each company's jobs; jobs that drop out of the sitemap are never expired because of it. Job pages are read
only when the URL matches your keywords (at most `max_pages` per run).

Requests are rate-limited per host, respect robots.txt, and retry within bounds. A source that fails is reported
(System, Telegram) without touching existing jobs. A job is *active* while the last successful run of its source
still lists it: that is "still listed", not proof the vacancy is open. Live-tested: Greenhouse (Milestone 10) and `sitemap` on the Visa, Mastercard and Adobe
Workday sites (Milestone 15); Lever, Ashby and `jsonld` are tested against recorded responses.

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
