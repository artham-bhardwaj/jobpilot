# JobPilot — implementation status

**Source of truth for what is verified.** Last updated: 2026-10-16, after Milestone 10's offline phases (304 tests passing).
Commits: `19f3d19` baseline, `04b70f3` Milestone 8, `cb9b23f` Milestone 9, then the Milestone 10 offline phases
("Complete milestone 10 reliability validation").
`docs/CHECKLIST.md` is superseded by this file.

Legend: **Verified** = exercised by automated tests and/or the live local checks below.
**Offline-verified** = tested only against recorded/synthetic responses; the live service has not been contacted.

## Milestone 8 — starting state (2026-10-15)

Milestone: real-world discovery, matching, resume selection, recruiter workflows, reliable job pipeline.

- Git: baseline commit `19f3d19` exists; uncommitted = the cross-origin/flash-message fix (§3a) + its test.
- Tests at start: **178 passed** (`.venv/bin/pytest`, 94 s).
- Your running server (port 8794): `/` 200, `/health` ok, schema 0005, `Referrer-Policy: same-origin`.
  The cross-origin form issue does **not** reproduce with the current code.
- Your data (`./data`, read-only inspection): 1 candidate-profile row, 1 audit entry, nothing else. It is preserved;
  new migrations take an automatic backup before upgrading a non-empty database.
- Reusable as-is: source adapters (Greenhouse/Lever/Ashby/JSON-LD/import), polite HTTP client with SSRF guard,
  dedup/provenance, parser, matcher, resume library/selection, contacts/DNC, deterministic drafts, application
  lifecycle with evidence gate, review items, Playwright assist, scheduler/task runner, backups, analytics.
- Gaps against this milestone: no per-run source history/health page or dry run; snapshot expiry could expire
  everything after one empty-but-valid response; no canonical apply-URL dedup; no explicit-vs-inferred provenance
  for parsed fields; incidental skill mentions can become "required"; fixed matching weights; no data-confidence
  assessment or hard-conflict flag; no calibration/feedback; no resume-selection review view; no contact↔job
  associations, duplicate-contact merge or company pages; no draft revision history; review queue lacks categories,
  filters, pagination and bulk actions; no preparation snapshot or answer bank; browser assist lacks per-site
  approval; analytics lack new-vs-duplicate, resume-by-category and skill-gap reports; scheduler thread isn't stopped
  on shutdown and has no "next run" display.

### Milestone 8 — progress log

| Stage | Result | Tests |
|---|---|---|
| A. Baseline | 178 passed; server healthy; your data preserved | 178 |
| B. Regressions | none found | — |
| C. Source engine | migration 0006 (verified on a copy of your DB: rows identical, auto-backup taken); canonical apply-URL dedup; duplicates from other sources add provenance but never overwrite (bug found and fixed); expiry only after N consecutive misses; empty or collapsed listings = "anomaly" (nothing expired); run status success/partial/unavailable/failed/anomaly; per-run history with 429 and HTTP-error counts; dry run (rolled back); Sources health and per-source history pages; job-data URLs fetched only on your explicit check | 200 |
| D. Parsing & matching | explicit/structured/inferred/unknown provenance per field; required/preferred/**mentioned-only** skills with snippets; employment type, deadline, "or equivalent experience"; configurable skill dictionary; 7 configurable weights; data-confidence level; hard vs soft conflicts (hard → "not suitable" whatever the score); unverified/learning/missing skills distinguished; work vs personal-project evidence; calibration page with feedback (recorded only), read-only weight preview, explicit save; 17 parsing regression fixtures. Fixed along the way: degree stated under "nice to have" was missed; inline "Requirements:" lines and Lever's "What you need" heading were not recognised; unknown info used to add up to borderline scores; `settings_store.reset` didn't flush | 230 |
| E. Resume selection | coverage counts only skills in the resume **and** confirmed in your profile; warnings for unconfirmed resume claims, confirmed skills absent from the resume, unsuitable or missing variants; read-only resume-selection review page | 236 |
| F. Review queue & preparation | central queue (12 categories, reasons, recommended next action, safe actions, filter/sort/pagination); bulk limited to mark-reviewed / shortlist / dismiss / acknowledge (never approve, send or submit); acknowledgements persist (stable ids); persisted application preparation record; answer bank (sensitive questions never suggested or auto-filled); application questions with per-answer approval; unanswered questions block "ready for review"; questions found by browser assistance are recorded; redirects restricted to local paths | 247 |
| — Interruption recovery | task was interrupted before the post-F full run. Recovered: tree compiles; **247 passed, 0 failed**; app starts on a fresh data dir (schema 0006, all pages 200). Your DB still at 0005 (migrates with backup when your server restarts). Half-wired: `ContactJob` read but never written, `DraftRevision` unused → Stage G | 247 |
| G. Recruiters & companies | contact↔job links (manual, from drafts, from postings); duplicate-contact finder and merge (explicit confirm; the kept contact's values win, blanks filled; do-not-contact never lost; "verified" kept only with a note; interactions/drafts/links moved; audited); company pages with editable notes and safe URLs; draft revision history (saved on every content change); drafts link to their application | 252 |
| H. Browser assistance | assist refuses unless you approve the exact host of the application URL (web: checkbox + host; CLI: `--approve-host`); optional `browser.autofill_sites` allow-list; approval audited; unknown and sensitive fields become application questions; still never submits or ticks consent (headless tests on a 127.0.0.1 page) | 257 |
| I. Analytics | collection by source (new vs duplicate, dry runs excluded, duplicate share only with a minimum sample); review-queue counts; resume selection by role category (exact/fallback/missing); common skill gaps; CSV exports `sources` and `skill_gaps` (formula-injection guard); dashboard "Pipeline" funnel (Applied counts only evidence-backed submissions) | 261 |
| J. Reliability | background scheduler thread stopped on app shutdown; job expiry only from explicit deadlines (vague "closing in January" ignored); "Next run" + task labels on the System page, with a note that nothing runs while the laptop sleeps; backup/restore keeps the new tables; a schema-0005 backup is upgraded to 0006 on restore | 267 |
| K. Acceptance | one end-to-end offline test across the 18 criteria (import → dedup → provenance → required/preferred → relevance vs eligibility with evidence → resume selection → queue → preparation record → recruiter draft → missing-info blocks → browser stops before submit → evidence-gated "applied" → security → backup/restore → pages) plus a check that no AI/LLM SDK is a dependency or import. **Full suite: 269 passed, 0 failed, 0 skipped (142 s).** | 269 |

## Milestone 9 — live validation & operational readiness (offline part complete)

No live external request was made. Your data in `./data` was not modified: the DB SHA-256 is unchanged and it is
still at schema 0005, with 1 profile row and 1 audit entry. All checks on your data used copies in the session scratch area.

### Verification levels

| Level | What |
|---|---|
| Automated tests | **294 passed, 0 failed, 0 skipped, 0 errors**. Baseline before Milestone 9: 269/0/0/0. The full suite also passes inside a network namespace with only loopback (`unshare -rn`), so **no test can reach an external host**. |
| Local runtime (scratch data dir, port 8799) | `/health` ok; all main pages 200; `/docs`, `/redoc`, `/openapi.json`, `/api/docs` 404; bound to 127.0.0.1. Remote binds (`0.0.0.0`, LAN IP, `::`) refused with exit 2. Foreign `Host` 400; foreign `Origin`/`Referer` and `Origin: null` 403; `..` paths under `/static` 404. Scheduler enabled in-process: health shows it, System page shows "Next run", SIGTERM exits in 0.2 s. |
| Copy of your real DB | `db upgrade` 0005→0006: profile and audit rows byte-identical, `integrity_check` ok, no FK violations, 0600 `pre-migrate-0005` backup. Restoring that backup with `backup restore`: rows identical, re-migrated to 0006, all 5 new tables present, 0600 pre-restore backup taken. |
| Oversized/malformed input (local) | 300 MB multipart upload: "File too large", server peak RSS ~112 MB. 300 MB urlencoded body: 400 in 18 ms. 5 MB text field: 400 ("exceeded maximum size of 1024KB"). Cross-origin 300 MB upload: 403 before the body is read. |
| Synthetic/offline only | Every source adapter, Gmail, and browser assist (local test pages). See the adapter table below. |
| Live external service | **None.** |

### Defects found and fixed (each has a regression test in `tests/test_m9_validation.py`)

1. **`jobpilot db current` silently migrated the database**, and took a backup, so it could never show a pending
   migration. Reproduced on a copy of your 0005 DB. Now `current` is read-only and prints
   `{"current", "head", "pending"}`; `upgrade` migrates as before.
2. **Lever boards larger than the page cap (2,200 postings) were treated as complete listings**, so postings past
   the cap counted as "missing" and were expired after 2 runs. The 50% mass-disappearance check doesn't catch a
   ~12% shortfall. Now a capped listing is a `partial` run that expires nothing. Request count is unchanged (22 pages).
3. **Editing a draft after its Gmail draft was created made "Send via Gmail" send the old Gmail draft.** Editing
   correctly revoked approval, but re-approving and sending reused the stale `gmail_draft_id`, so the text you
   didn't approve was sent. Now a content change drops the stale reference, so sending builds a fresh Gmail draft
   from the re-approved text. The edit page warns that the old copy in Gmail Drafts is outdated (delete it there),
   and the audit log records its id.

Before the fixes, the tests for defects 2 and 3 fail when run against the committed code (`git archive HEAD`).
Defect 1 was reproduced manually before the fix.

### Additional regression tests (passed, no defect found)
- Failed, unavailable, malformed or anomalous runs leave the `jobs`, `job_sightings` and `companies` rows **identical**
  (full-row comparison). Covers 429, 503, timeout, connection error, 404, HTML instead of JSON, `jobs: null`, a non-object
  job entry, a wrongly typed field, and an empty board.
- A bug partway through an ingest rolls back updates already applied to existing jobs. A dry run against existing
  jobs changes nothing.
- Removing location, work mode, experience or salary from a posting never raises relevance or confidence
  (95.9 → 89.9 → 83.9 as information is removed). Skills mentioned only in "About us"/"Benefits" are not mandatory.
- A do-not-contact entry added *after* approval blocks the Gmail send, and nothing reaches Gmail.

### Already covered by Milestone 8 tests (re-run, all passing)
- **Matching:** confirmed / learning / unverified / missing skills; work vs personal-project evidence; hard conflicts
  override high relevance; resume coverage counts only skills that are both in the resume and confirmed; missing or
  unsuitable resume variants are warned about.
- **Browser assist:** exact-host approval (web and CLI); unknown and sensitive fields recorded as questions; consent
  boxes untouched; never submits (headless Chrome, local page).
- **Redirects:** local-only.
- **Email:** sending needs approval plus a typed `SEND`; daily send limit; draft revisions are kept.
- **Gmail tests:** stubbed Google endpoints only.

### Source adapters (none live-tested; all offline with synthetic/recorded fixtures)

All network fetches go through `PoliteHttp`:
- 20 s timeout; 2 retries on 429/5xx, honouring `Retry-After` (capped at 60 s);
- ≥2 s between requests to the same host; User-Agent `JobPilot/0.1 (personal job-search assistant)`;
- robots.txt: 401/403 → disallow; other 4xx → allow; oversized → disallow; unreachable → proceed;
- 5 MB response cap; each redirect hop re-validated (max 5); private/loopback addresses refused.

| Adapter | Input / config | Requests per run | Snapshot & expiry | Known restrictions |
|---|---|---|---|---|
| Greenhouse | `{"board_token": "<co>"}` → `boards-api.greenhouse.io/v1/boards/<co>/jobs?content=true` | robots.txt + 1 | full snapshot; expiry after 2 consecutive misses; empty board or >50% vanish (≥10 active) = anomaly | real response shape not yet compared; job-board URLs (`boards.`/`job-boards.`, EU) recognised for single-job add |
| Lever | `{"site": "<co>"}` → `api.lever.co/v0/postings/<co>?mode=json` | robots.txt + 1 per 100 postings (max 22) | full snapshot below 2,200 postings; above that partial (fixed above) | EU Lever (`api.eu.lever.co`) boards not supported for collection |
| Ashby | `{"board": "<co>"}` → `api.ashbyhq.com/posting-api/job-board/<co>` | robots.txt + 1 | full snapshot; unlisted jobs skipped | fixture built from public docs, not a recorded response |
| JSON-LD pages | `{"urls": [...]}` (≤50 individual job pages) | robots.txt + 1 per URL | never a full snapshot; a page expires only after consecutive 404/410 | no JS-rendered or login-walled pages; listing pages not crawled |
| Add by URL | Greenhouse/Lever URLs use the API; others use page JSON-LD | 1–2 | n/a | first posting only if a page has several |
| CSV/JSON import | file upload, ≤2,000 rows, ≤10 MB, column aliases | none | n/a (manual) | rows without a title skipped and reported |

**Recommended first live test (needs your authorisation):** a Greenhouse board you care about, as a **dry run** first.
Expected requests: `GET https://boards-api.greenhouse.io/robots.txt`, then
`GET https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true`. That's 2 requests, nothing saved.
Then one real run (2 requests), and a second real run later to confirm stability and that nothing expires wrongly.
Compare 5 stored jobs against their public pages for title, location, description and apply URL.

## Milestone 10 — real-world validation & application readiness (offline phases complete; live phases await authorisation)

**No external service was contacted.** Your `./data` is unchanged: DB SHA-256 identical, still schema **0005**
(1 profile row, 1 audit entry; no skills, work history, resumes or sources), `backups/` and `files/` empty.
It was not migrated: nothing live has run against it yet, and migrating only to turn the health check green is not a reason.

### Results

| Check | Result |
|---|---|
| Full suite (inside a loopback-only network namespace) | **304 passed, 0 failed, 0 skipped, 0 errors, 173.9 s** (Milestone 9 baseline: 294) |
| Milestone 9 review | Committed as `cb9b23f` after reviewing the staged diff and scanning it for secrets and personal data. Within JobPilot, `edit_draft` is the only code path that changes a draft's recipient, subject or body, so a Gmail draft created before an edit cannot be sent afterwards. The attached resume is fixed when the draft is created. Edits made **inside Gmail** are sent as they are in Gmail (your own edit, not stale content). |
| Acceptance (`tests/test_m10_acceptance.py`) | One synthetic flow covering all 20 criteria, including: Gmail draft created → draft edited → send refused until re-approved → only the new draft (`d-2`, new text) is sent; wrong-host browser approval refused; headless Chrome fills the synthetic form and the page server receives **no POST**; an anomalous then a failed source run expire nothing; backup and restore keep the jobs, the "applied" application, run history and draft revisions. |

### Defects found and fixed (tests in `tests/test_m10_operations.py`; both fail on `cb9b23f`)

1. **A task interrupted by a crash, `kill -9` or power loss stayed "running" forever**, with no finish time, and
   `/health` kept reporting "last task failure: none". Reproduced on scratch data. Now an orphaned run (one whose
   task lock is free) is recorded as failed ("Interrupted…"). This happens when the same task next runs, and at app
   start-up for runs older than a minute. Runs still genuinely in progress, i.e. with their lock held, are left alone.
2. **A negative or `NaN` `Retry-After` on a 429 crashed the retry path** (`time.sleep` raised `ValueError`), so a
   temporary rate limit was recorded as a `failed` source. Such values now fall back to the normal backoff and the
   run is `unavailable`. HTTP-date values already worked; they now have a regression test too.

New coverage without defects: assist pauses without filling on a **login wall** and on an **MFA/verification-code**
prompt (headless Chrome; previously only CAPTCHA was tested). The test page server now records requests, so tests
can prove no form submission happened.

### Integration classification

| Integration | Classification |
|---|---|
| Source engine (dedup, provenance, expiry, partial/anomaly, dry run, run history, failure isolation) | Automated-test verified (synthetic) |
| Greenhouse | Synthetic/offline verified; **blocked by authorisation** for live (see request below) |
| Lever, Ashby, JSON-LD pages | Synthetic/offline verified; not tested live |
| CSV/JSON import, add by URL | Automated-test verified (synthetic files; URL fetch offline only) |
| Matching, eligibility, resume selection, application preparation | Automated-test verified (synthetic profile). Real-profile calibration: **not possible yet**, because your profile has no skills or experience and there are no resumes |
| Browser assistance | Automated-test verified on local synthetic pages (headless Chrome). Real application form: not tested, blocked by authorisation |
| Gmail | Synthetic/offline verified (stubbed Google endpoints). Live: not tested, blocked by authorisation and your own OAuth client |
| Scheduler, task history, crash recovery | Automated-test verified; local runtime verified (Milestone 9: start with scheduler, SIGTERM exit 0.2 s) |
| Backup/restore/migration | Automated-test verified; verified on a copy of your real DB (Milestone 9) |
| Local-only binding, CSRF, Host checks | Automated-test verified; local runtime verified |
| systemd timer | Unit generation tested; not installed (needs your approval) |
| JS-rendered, login-walled or CAPTCHA-protected application sites | Unsupported by design (assist pauses and hands over to you) |

### Remaining risks
- Real Greenhouse responses may differ from the fixture.
  - A board whose JSON with `content=true` exceeds the 5 MB response cap fails safely: nothing is saved or expired.
    In that case a smaller board is needed, or a reviewed change to the cap.
  - If `boards-api.greenhouse.io/robots.txt` disallows the API path, JobPilot refuses to fetch it, by design.
- Hosted application forms often embed an invisible reCAPTCHA iframe. Assist would then **pause on page load**: safe,
  but it fills nothing. Only a live, approved test can show how often this happens.
- The Gmail daily limit counts from UTC midnight. An old Gmail draft left in Gmail after an edit has to be deleted by you.
- No per-run job limit: a source run imports the whole board. To keep the first live test bounded, run it against a
  scratch data directory and review the results before importing into your real data.

### Authorisation request: first live source (nothing has been contacted)

| # | Item | Value |
|---|---|---|
| 1 | Company | **to be confirmed by you**. Suggestion from general knowledge, not checked live: GitLab (remote-first and hires in India; its public board has historically been on Greenhouse). Any company whose careers page links to `boards.greenhouse.io/<token>` or `job-boards.greenhouse.io/<token>` works. |
| 2 | Board identifier | `<token>` from that URL, configured as source type `greenhouse`, config `{"board_token": "<token>"}` (Sources → Add). Board URLs aren't accepted, only the token. |
| 3 | Hostnames contacted | `boards-api.greenhouse.io` only |
| 4 | Requests | `GET /robots.txt`, then `GET /v1/boards/<token>/jobs?content=true` (≥2 s apart) |
| 5 | robots.txt | Yes, fetched first and obeyed. A 401/403 or an oversized file means no fetch. |
| 6 | Maximum listing requests | 1 per run (plus up to 2 retries only on 429/5xx, honouring `Retry-After` ≤ 60 s). Planned: dry run, run, re-run = **at most 3 listing requests + 3 robots.txt fetches** (robots.txt is fetched once per run). |
| 7 | Data stored | Dry run: only a run record (counts, status). Real run: each posting's title, location, description (text and raw HTML), apply URL, Greenhouse job id, department and posted date; a sighting with source and method; parsed requirements and match scores. No personal data is sent anywhere. |
| 8 | Changes your database? | **No**: proposed against a scratch copy of your data directory (`JOBPILOT_DATA_DIR=<scratch>`). Your real DB is only touched after you've reviewed the results and asked for it. |
| 9 | Can it expire jobs? | Not on the first run (nothing exists yet). On the re-run, a job expires only after 2 consecutive successful runs omit it, and empty, failed or mass-disappearance responses expire nothing. |
| 10 | How to stop safely | Ctrl-C at any point. The run is one transaction, so an interrupted run saves nothing. Disable or delete the source in Sources to prevent further runs (its jobs are kept). No scheduler is enabled. |

## Summary (Stage 7 results)

| Check | Before Stage 7 (audit) | After Stage 7 |
|---|---|---|
| Automated tests | 87 passed, 3 warnings | **178 passed** (173 at end of Stage 7; +5 from the §3a fixes), 0 warnings |
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
| Spawning `jobpilot assist` from the web UI with browser enabled | Not run (only the disabled path, the host-approval refusal and the CLI error path are tested). |
| Milestone 8 source health/expiry against real boards (429s, real empty boards, reposts) | Offline-verified only. |
| Matching calibration on real postings | Not done; weights are defaults until you review real jobs on `/matching`. |
| systemd user timer installed and firing on your machine | Unit files generated and tested; not installed. |
| PDF generation | Verified with the local Chrome (headless). Not tested on other machines. |

## 3. Remaining limitations

- **No app authentication.** Any local process can use the app on 127.0.0.1. CSRF protection relies on browser
  `Origin`/`Referer`/`Sec-Fetch-Site` headers.
- **DNS rebinding window**: the address check and the actual connection resolve DNS separately, so a hostile DNS
  server could switch answers in between. Pinning the connection to the checked IP would close this. Low risk for a
  single-user tool that only fetches URLs you configure.
- Backups are **not encrypted** (0600 files in a 0700 directory). Keep them private; anyone who can read them can
  read everything JobPilot stores.
- Your database (`./data`) is still at schema 0005 (1 profile row, untouched). Your earlier server on port 8794 is no longer running; the next `jobpilot serve` (or any command except `db current`) migrates it to 0006, taking an automatic backup first. This was verified on a copy (Milestone 9).
- The Gmail daily send limit counts from midnight **UTC** (05:30 IST), not local midnight.
- After editing a draft that already had a Gmail draft, the old copy stays in your Gmail Drafts folder until you
  delete it. JobPilot never sends it.
- Large file uploads are spooled to a temporary file by the web framework before JobPilot rejects them (>10 MB).
  Memory stays bounded; this needs a same-origin request from your own browser.
- Parsing is rule-based: unusual posting layouts may leave requirements as "unknown" (scored 0, flagged low confidence)
  rather than guessed. Contact duplicate detection uses email/name+company only.
- Scheduled tasks don't run while the laptop is asleep or the server is stopped; they catch up on the next due check.
- The umask change applies to the whole JobPilot process (intended: everything it writes is private).
- Skill extraction uses a fixed ~90-entry dictionary; no currency conversion; interview time zones are labels.
- Gmail interview import matches applications by company name in the message text.
- No JavaScript-rendered or login-walled career sites.
- `services/registry.py` still has `ImportError` fallbacks that can no longer trigger (harmless; left as-is).
- Test suite takes about 2 minutes (subprocess CLI tests and headless Chrome tests dominate).

## 3a. Post-Stage-7 fixes (reported by user, 2026-10-15)

- **Every form save failed with "Cross-origin request blocked" in real browsers.** Cause: the Stage 1 security header
  `Referrer-Policy: no-referrer` makes Chrome send `Origin: null` on form posts, which the CSRF check correctly
  rejects. Missed because TestClient doesn't emulate browser Origin behaviour, and the earlier Chrome checks only
  loaded pages without submitting forms. Fix: `Referrer-Policy: same-origin`. That still sends no referrer to
  external sites, and the CSRF check is unchanged. Null-origin blocks are now logged.
  Regression test: `tests/test_browser_forms.py` submits forms in headless Chrome against a live server.
- **Flash messages after anchor redirects were never shown** (`/profile#skills?msg=…`: the query was placed
  inside the fragment), including error messages. Fixed in `redirect()`, with a unit test.
- Tests: **178 passed**.

## 4. Failed tests and open defects

- **Failed tests: none** (304/304 after Milestone 10's offline phases, run with the network removed).
- **Open defects: none known.** Milestone 9's three and Milestone 10's two defects are fixed with regression tests. All of D1–D8 and S1–S3/S5 from the audit are fixed with regression tests.
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

1. **One live job board, read-only.** Pick one company whose Greenhouse board you care about. Run a dry run first
   (exact requests listed in the Milestone 9 section), then `jobpilot collect`. Compare the stored jobs with the public pages for:
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
