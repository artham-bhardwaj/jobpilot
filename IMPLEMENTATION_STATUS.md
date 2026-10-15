# JobPilot — implementation status

**Source of truth for what is verified.** Last updated: 2026-10-15, after Stage 7 (hardening).
Repository has no commits; nothing is tracked by Git. `docs/CHECKLIST.md` is superseded by this file.

Legend: **Verified** = exercised by automated tests and/or the live local checks below.
**Offline-verified** = tested only against recorded/synthetic responses; the live service has not been contacted.

## Summary (Stage 7 results)

| Check | Before Stage 7 (audit) | After Stage 7 |
|---|---|---|
| Automated tests | 87 passed, 3 warnings | **173 passed, 0 failed, 0 skipped, 0 warnings** (`ResourceWarning` now fails the suite) |
| Tests run from a different working directory | 1 failure | **173 passed** (from `/tmp`, JUnit: errors=0 failures=0 skipped=0) |
| Route probe: 93 handlers, missing IDs / empty forms | 6 × HTTP 500 | **0 × 5xx** (original audit script, unchanged) |
| Route probe: 33 targeted invalid requests | 8 × HTTP 500 | **0 × 5xx** (original audit script, unchanged) |
| Line coverage (excl. migrations; CLI runs in subprocesses, not counted) | 79% | **84%** |
| Fresh editable install per README | works | works; `pip check` clean; `init` output quiet |
| Non-editable install (`pip install .`) | works | works (see §6 note on one unreproduced build failure) |
| `/health`, dashboard (live server) | 200 / 200 | 200 / 200; health now includes a `data_permissions` check |
| Data file permissions (umask 0002) | dir 0775, DB 0644, log 0664 | **dirs 0700; DB, log, backups 0600** |
| `/api/docs` | broken (CDN blocked by CSP) | **404** (disabled); `/openapi.json` opt-in only |
| Secrets / personal data in repo | none | none (pattern scan: no keys/tokens; test credentials are synthetic) |

## 1. Completed and verified

Everything listed as verified in the previous audit still passes, plus the following Stage 7 work.

**Route robustness (D1 fixed)**
- All path-parameter lookups go through `get_or_404` (42 call sites), so a missing record returns **404** with a readable HTML page.
- Unknown profile section → 404 listing the valid sections. Malformed backup name → 400; missing backup → 404.
- FastAPI's raw 422 JSON replaced by a 400 HTML page for invalid query/form/path input.
- A last-resort handler returns a generic 500 page with no internal details, and logs the traceback locally.
- Regression tests: all 14 reported invalid requests (12 handlers), the same handlers with valid requests,
  invalid query strings, and a sweep of every registered route (`tests/test_hardening_routes.py`).

**Security**
- **S1 fixed**: umask 0o077; `data/` and sub-directories 0700; DB/WAL/SHM, logs, backups, exports and tokens 0600.
  Existing loose permissions are tightened at start-up (tested), and `/health` reports any that remain.
- **S2 fixed**:
  - Redirects are no longer followed automatically. Each hop (max 5) is re-validated before it is contacted.
  - Allowed addresses must be globally routable: loopback, private, link-local, CGNAT, reserved, multicast and
    IPv4-mapped IPv6 are refused, as are hostnames that resolve to *any* such address.
  - URLs with credentials and non-http(s) schemes are refused.
  - All tests now run with the guard **on**, using a fake DNS resolver. Before, the guard was off in tests.
- **S3 fixed**: responses are streamed with hard caps (5 MB; robots.txt 512 KB). An oversized `Content-Length` is
  rejected before any body is read. An unusable or oversized robots.txt is treated as "disallow".
- **S5 / D3 fixed**: Swagger/ReDoc are not served; `/openapi.json` only with `JOBPILOT_ENABLE_OPENAPI=true`.
- Backup restore now limits total uncompressed size (2 GB) and member count. Tested: backups exclude the OAuth
  token and the browser profile.
- Still in place: localhost-only bind, trusted hosts, same-origin POST check, CSP. Verified live.

**Reliability**
- **D2 fixed**: no test depends on the working directory (full suite passes from `/tmp`).
- **D4 fixed**: CLI console logging is WARNING-level; Alembic chatter removed (`init` prints only its summary).
- **D5 fixed**: refused remote bind is a clean message with exit code 2. Expected CLI failures print one line;
  `--debug` shows the traceback.
- `python -m jobpilot.cli` no longer loads the CLI module twice (that broke error handling); `python -m jobpilot` added.
- **D6 fixed**: GET pages no longer write to the database. A test hashes the whole DB, crawls 28 GET pages on a
  seeded DB that includes the previously write-triggering states, and asserts no change. In detail:
  - the profile is read-only until you save it;
  - follow-up suppression moved to the scheduled follow-up check;
  - built-in templates are seeded at start-up;
  - checklists are computed for display, not saved;
  - unscored jobs show "Re-score" instead of re-scoring on view.
  - Intentional exception: downloading the full JSON export writes an audit-log entry.
- **D8 addressed**:
  - every command except `init` refuses to create a database implicitly, and explains why;
  - `JOBPILOT_ENV_FILE` selects a specific config file;
  - `init` and `serve` print the resolved config and data locations.
- **D7 fixed**: closed the upload temp file left open by the evidence handler. The third-party Starlette/httpx
  deprecation notice is filtered in pytest config, with a comment explaining why.

**Defects found by the new tests, and fixed**
- Gmail body parsing returned both the plain and HTML copies of `multipart/alternative` mail; it now picks one.
- Interview-invitation stage detection: `hr@…` in the sender address made technical interviews classify as "HR".
  Addresses and links are now ignored, and more specific stages take precedence.
- A browser launch failure escaped `run_assist` and left the run marked "running" forever. It is now recorded as
  `failed` with an explanation.
- `test_review_flags` asserted `needs_review in (True, False)`, which could never fail. It now checks real behaviour.

**Offline test gaps closed** (synthetic fixtures, fake HTTP)
- JSON job import, including malformed rows, `javascript:` URLs and bad numbers.
- JSON-LD multi-URL source: structured page, partial-HTML fallback, 404, and a private address, all in one run.
- `check_alive` (404/410 → expired; 200; 500 → "could not verify"; no URL).
- Lever single-posting URL → API.
- Ashby adapter: listed/unlisted, remote/hybrid, malformed response does **not** expire existing jobs.
- Scheduler: `collect_jobs`, `followup_check` and `gmail_sync` tasks; failed task retried once and stays due;
  background thread starts, runs a due task and stops.
- Gmail:
  - token refresh on expiry, a single refresh on 401, refresh failure, revoke;
  - body parsing (plain/HTML/nested);
  - interview import (requires opt-in; creates suggestions only);
  - web actions;
  - **the full loopback OAuth flow**, with a simulated browser redirect to the local listener and a state-tampering
    rejection test.
- Browser assist: launch failure; the supported-site path end-to-end in headless Chrome (fields filled,
  confirmation page captured as candidate evidence, application **not** marked applied).
- Other: salary parsing (USD k, INR LPA, EUR), the contact do-not-contact toggle, draft cancel, resume archive,
  settings reset, web-triggered source run, and the 500 safety net.
- CLI exercised end-to-end:
  - `collect`, `add-url` (error paths), `rematch`, `assist` (error path);
  - `db current`, `db upgrade`;
  - `scheduler run`, `run-due`;
  - `gmail sync`, `gmail revoke`;
  - `export all-json`;
  - `backup create`, `list`, `restore` (abort, yes, missing);
  - `delete-data` for every scope, with and without confirmation.

## 2. Implemented but not verified against real services

Offline tests passing does **not** mean these work live. None has contacted a real external service.

| Item | Status |
|---|---|
| Greenhouse / Lever / Ashby public APIs | Offline-verified only. Real response shapes not yet compared (Ashby fixture built from public docs). |
| JSON-LD career pages on real employer sites | Offline-verified only. Real pages vary widely. |
| Gmail OAuth, drafts, send, reply sync, interview import | Offline-verified only (mocked Google endpoints, simulated browser). Needs your Google Cloud OAuth client. |
| Browser assist on a real Greenhouse/Lever form, headed, with you completing it | Not run. Headless synthetic-form tests only. |
| Spawning `jobpilot assist` from the web UI with browser enabled | Not run (only the disabled path and the CLI error path are tested). |
| systemd user timer installed and firing on your machine | Unit files generated and tested; not installed. |
| PDF generation | Verified with the local Chrome (headless). Not tested on other machines. |

## 3. Remaining limitations

- **No app authentication.** Any local process can use the app on 127.0.0.1. CSRF protection relies on browser
  `Origin`/`Referer`/`Sec-Fetch-Site` headers.
- **DNS rebinding window**: the address check and the actual connection resolve DNS separately, so a hostile DNS
  server could switch answers in between. Pinning the connection to the checked IP would close this. Low risk for a
  single-user tool that only fetches URLs you configure.
- Backups are not encrypted (0600 files in a 0700 directory).
- The umask change applies to the whole JobPilot process (intended: everything it writes is private).
- Skill extraction uses a fixed ~90-entry dictionary; no currency conversion; interview time zones are labels.
- Gmail interview import matches applications by company name in the message text.
- No JavaScript-rendered or login-walled career sites.
- `services/registry.py` still has `ImportError` fallbacks that can no longer trigger (harmless; left as-is).
- Test suite takes about 2 minutes (subprocess CLI tests and headless Chrome tests dominate).

## 4. Failed tests and open defects

- **Failed tests: none** (173/173, also when run from another directory).
- **Open defects: none known.** All of D1–D8 and S1–S3/S5 from the audit are fixed with regression tests.
  S4 (no auth) and S6 (unencrypted backups) are accepted limitations (§3).
- One non-editable `pip install .` attempt failed during verification and could not be reproduced in three
  subsequent clean attempts. Its error message was hidden by `-q | tail -1`. Most likely cause: the isolated
  build step fetching `setuptools` from PyPI.

## 5. Security review notes (Stage 7)

- No `shell=True`, `eval`, `pickle`, `yaml.load` or `verify=False`. Jinja autoescape on; sandboxed email templates.
- Error pages show only controlled messages; tracebacks go to the local log only.
- No secrets in the tree. A pattern scan for AWS/Google/GitHub/Slack/OpenAI keys and private keys found nothing.
  The test OAuth values are synthetic strings ("rt", "at", "cs").
- Personal-data scan: no owner-identifying strings in the repository.
- `.env`, `data/`, tokens, client secrets, browser profile and generated systemd units are ignored. 138 files would
  be added by `git add .`, all source, tests, docs or config.

## 6. Exact verification commands

```bash
# Tests (repo)
.venv/bin/pytest -rs                                     # 173 passed, 0 warnings

# Fresh copy of the working tree (outside the repo), install per README, umask 0002
git ls-files --others --exclude-standard | tar -cf - -T - | (cd $V/fresh && tar -xf -)
cd $V/fresh && umask 0002 && python3 -m venv .venv && .venv/bin/pip install -e ".[dev,browser]" && .venv/bin/pip check
cp .env.example .env && .venv/bin/jobpilot init          # quiet; prints config/data locations; schema 0005
stat -c "%a %n" data data/jobpilot.db data/logs data/logs/jobpilot.log data/files data/backups data/secrets
                                                          # 700/600/700/600/700/700/700

# Tests from another working directory
cd /tmp && $V/fresh/.venv/bin/pytest --rootdir=$V/fresh -c $V/fresh/pyproject.toml $V/fresh/tests --junitxml=$V/junit.xml
                                                          # tests=173 errors=0 failures=0 skipped=0

# Live server
.venv/bin/jobpilot serve --port 8794 &
curl -s http://127.0.0.1:8794/health                     # 200, ok=true, data_permissions owner-only
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8794/                       # 200
for u in /api/docs /docs /redoc /openapi.json; do curl -s -o /dev/null -w "$u %{http_code}\n" http://127.0.0.1:8794$u; done   # all 404
curl -H "Host: evil.example" http://127.0.0.1:8794/      # 400
curl -X POST -H "Origin: https://evil.example" http://127.0.0.1:8794/system/backup  # 403
curl -X POST http://127.0.0.1:8794/applications/999999/status -d to=withdrawn       # 404 page
ss -ltn | grep 8794                                      # 127.0.0.1 only

# Original audit probes (unchanged scripts)
.venv/bin/python $AUDIT/probe.py                         # 93 handlers, 0 × 5xx
.venv/bin/python $AUDIT/probe2.py                        # 33 cases, 0 × 5xx

# Coverage (scratch venv)
.venv/bin/coverage run --source=src/jobpilot -m pytest && .venv/bin/coverage report --omit="*/migrations/*"   # 84%

# Non-editable package
python3 -m venv $V/wheel1 && (cd $V/fresh && $V/wheel1/bin/pip install .)
JOBPILOT_DATA_DIR=$V/wheel1/d $V/wheel1/bin/jobpilot export jobs      # exit 2: "No JobPilot database … run jobpilot init"
JOBPILOT_DATA_DIR=$V/wheel1/d $V/wheel1/bin/jobpilot init && $V/wheel1/bin/python -m jobpilot db current
JOBPILOT_DATA_DIR=$V/wheel1/d $V/wheel1/bin/jobpilot serve --port 8793 &   # pages 200, /api/docs 404, 30 templates, 5 migrations

# Diff / secrets review against the pre-Stage-7 snapshot
diff -rq $BASELINE $CURRENT                               # 36 files changed, 11 added, 0 deleted; all Stage 7 items
grep -rnIE "(AKIA…|AIza…|ya29\.|-----BEGIN .*PRIVATE KEY|ghp_|xox[baprs]-|sk-…|GOCSPX-)" .   # none
```

`$V`, `$AUDIT`, `$BASELINE` and `$CURRENT` are directories in the session scratch area, outside the repository.
The pip installs above downloaded packages from PyPI. No other external network requests were made: all job-board,
Gmail and web tests use fake HTTP sessions and a fake DNS resolver.

## 7. Next steps: real-world testing (each needs your go-ahead)

Do these one at a time, with your approval before any external request:

1. **One live job board, read-only.** Pick one or two companies whose Greenhouse or Lever boards you care about.
   Run `jobpilot collect` against them, then compare the stored jobs with the public pages for:
   - title, location, description, apply URL;
   - expiry on a second run;
   - the rate limiting and robots.txt behaviour shown in the logs.
2. **Your real resume.** Import it into one resume slot, review the extracted text and detected skills (these
   become *unverified* suggestions), approve it, and check resume selection on a few real jobs.
3. **Profile and matching calibration.** Enter and confirm your real profile facts. Review 10–20 real jobs'
   relevance and eligibility explanations, then adjust thresholds and role keywords in Settings.
4. **One assisted application, headed, stopping before submit.** Enable `JOBPILOT_BROWSER_ENABLED`, run assist on
   one real Greenhouse/Lever posting, check which fields were filled or paused, then close without submitting.
5. **Gmail (optional).** Create your own OAuth client (docs/GMAIL_SETUP.md), run `jobpilot gmail auth`, create one
   Gmail draft to yourself, check reply detection, then decide whether to keep it connected.
6. **Scheduling (optional).** Install the systemd user timer and confirm a scheduled run appears in System → Task runs.
7. Optional hardening if you want it: pin outbound connections to the validated IP (closes the DNS-rebinding window);
   an optional local password for the web UI; encrypted backups.
