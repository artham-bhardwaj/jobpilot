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

1. **Profile** → fill in what you want to use, then *Confirm details*. Add skills and mark the ones
   you can genuinely claim as *confirmed*.
2. **Resumes** → *Create standard resume slots* (Master, Java Backend, SDE I, General SWE, Python
   Backend, AI/ML) → import your existing PDF/DOCX into each → review → *Approve*.
3. **Settings** → adjust `search_preferences` (keywords, exclusions, locations, levels, salary) and
   `matching` thresholds.
4. **Sources** → add company boards, e.g. Greenhouse `{"board_token": "<company>"}` or Lever
   `{"site": "<company>"}` → *Run*. Or paste a job URL / import a CSV under **Jobs → Add**.
5. Work from **Overview**, **Jobs**, and the **Review queue**.

See [docs/USER_GUIDE.md](docs/USER_GUIDE.md) for the full workflow.

## Command line

```bash
jobpilot serve [--port N]                 # dashboard
jobpilot collect [--source ID ...]        # run job sources now
jobpilot add-url URL [--company NAME]     # add one job posting
jobpilot import-jobs FILE.csv|json        # import jobs
jobpilot rematch                          # re-score active jobs
jobpilot assist APPLICATION_ID            # open browser to help fill a form (never submits)
jobpilot backup create|list|restore --name FILE
jobpilot export jobs|applications|contacts|drafts|interviews|summary|all-json [--out FILE]
jobpilot delete-data profile|contacts|outreach|everything [--include-backups]
jobpilot scheduler run-due|status|run --task NAME|systemd [--out DIR]
jobpilot gmail auth|status|sync|revoke    # optional, see docs/GMAIL_SETUP.md
jobpilot db upgrade|current
jobpilot --debug <command>                # show a full traceback if something fails
```

`python -m jobpilot …` works the same as `jobpilot …`.

## Tests

```bash
.venv/bin/pytest            # 173 tests, offline (fake HTTP, fake DNS), synthetic fixtures only
```

A few tests drive your local Chrome headlessly (PDF rendering, form-fill safety); they are skipped
automatically if Chrome/Playwright is unavailable. Passing offline tests do **not** prove that live
job boards, Gmail or employer forms behave the same way. See `IMPLEMENTATION_STATUS.md`.

## Optional scheduling

Scheduled tasks (collect jobs, follow-up check, maintenance, backup, Gmail reply sync) are **off**
by default. Enable each in **Settings → scheduler**, then either:

- set `JOBPILOT_SCHEDULER_ENABLED=true` so `jobpilot serve` runs them while it is open, or
- install the systemd user timer (runs while you're logged in; `Persistent=true` catches up after
  the machine was off):

  ```bash
  .venv/bin/jobpilot scheduler systemd --out deploy/systemd
  mkdir -p ~/.config/systemd/user && cp deploy/systemd/jobpilot-scheduler.* ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now jobpilot-scheduler.timer
  ```

Overlapping runs are prevented with per-task file locks.

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
