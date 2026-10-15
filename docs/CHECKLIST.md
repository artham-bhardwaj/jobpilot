> **Superseded.** This was the build-time checklist. Some items marked `[x]` here were later found to be
> only partially verified. The verified, current status is in [../IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md).

# Implementation checklist and status

Legend: **[x]** implemented and covered by automated tests · **[~]** implemented, partially
verified (see note) · **[P]** pending your credentials/manual step · **[ ]** not implemented.

Test suite: `.venv/bin/pytest` → **87 passed** (offline; synthetic fixtures; includes headless
Chrome tests that skip if Chrome/Playwright is unavailable).

## Stage 1 — Foundation
- [x] Package, editable install, `jobpilot` CLI, env configuration, `.env.example`
- [x] Localhost-only bind guard; trusted-host + same-origin (CSRF) middleware; security headers
- [x] SQLite (WAL, FKs) + Alembic migrations 0001–0005; model/migration drift test
- [x] Automatic backup before migrating a non-empty DB; migration preserves rows across a table rebuild (tested)
- [x] Structured JSON logging (rotating), log viewer, audit log
- [x] Task runner: per-task locks, bounded retries, run records
- [x] Backup / restore (moved forward from Stage 6 because migrations depend on it)

## Stage 2 — Profile, resumes, jobs, matching
- [x] Optional candidate profile; facts with confirmed / learning / unverified status; re-confirmation after edits
- [x] Resume library: slots per category, immutable versions, sha256, draft → approve, archive
- [x] PDF/DOCX/TXT validation and text extraction; skill suggestions recorded as unverified
- [x] Resume generation from selected confirmed facts → PDF via local Chrome (HTML fallback), validated
- [x] Sources: Greenhouse and Lever public posting APIs; JSON-LD career pages; manual URL (Greenhouse/Lever URLs via API); CSV/JSON import; manual entry
- [~] Ashby public posting API adapter — written to the documented API, not tested against a live board
- [x] Polite HTTP: timeouts, bounded retries (Retry-After), per-host rate limit, robots.txt, SSRF guard
- [x] Dedup (external id, apply URL, fingerprint), cross-source provenance, expiry (snapshot / validThrough / HTTP 404/410), repost detection
- [x] Parsing: skills (≈90-skill dictionary, "X or Y" alternatives), required vs preferred, experience, level, work mode, salary, sponsorship, degree, location constraints, emails
- [x] Classification into configurable role categories; separate relevance score and eligibility status, each explained; missing-info list; review flags

## Stage 3 — Explorer, resume selection, contacts, drafts
- [x] Job explorer: search, filters (category, eligibility, mode, score, recency, triage, status, source), sorting, pagination; job detail with explanations
- [x] Resume selection with category fallback and explicit "missing resume" flag
- [x] Contacts: provenance, verification status (guessed never verified), confidence, dedup/merge, history, do-not-contact list
- [x] Deterministic sandboxed templates (6 kinds); `[[MISSING]]` placeholders block approval; edit/approve/copy/mark sent; follow-ups (cap 2) suppressed on reply/DNC/closed application

## Stage 4 — Applications, review queue, browser assistance
- [x] 10-state lifecycle with validated transitions and history; duplicate detection with override note
- [x] Preparation checklist; resume version frozen once applied
- [x] Submission recorded only after approval + confirmation evidence + attestation
- [x] Human-review queue (review items + flagged jobs + drafts + unverified skills)
- [x] Playwright assist (Greenhouse/Lever): fills confirmed fields + resume; pauses on CAPTCHA/login/MFA/consent/legal/unknown; never submits; captures confirmation page as candidate evidence (fill logic tested on synthetic forms in headless Chrome)
- [~] Interactive headed session against a live employer form — not run (requires you and a real posting)
- [x] Manual workflow with copyable confirmed details

## Stage 5 — Gmail, interviews, skill gaps, analytics
- [x] Gmail client: OAuth loopback + PKCE, token 0600, minimal scopes, drafts, approved-only send with explicit confirmation, daily cap, reply sync, invitation scan (all tested with a mocked Google API)
- [P] Live Gmail authorisation — needs your Google Cloud OAuth client (docs/GMAIL_SETUP.md)
- [x] Interviews: manual entry, invitation extraction → confirmable suggestion, outcomes, feedback, follow-ups, thank-you drafts
- [x] Preparation packs per stage (DSA, Java, Spring Boot, SQL, LLD, HLD, behavioural, Python, ML)
- [x] Skill-gap analysis, learning backlog; mastery only via explicit confirmation with evidence
- [x] Analytics with minimum-sample rule and missing-vs-zero distinction; source and resume performance; 6 CSV exports with formula-injection protection

## Stage 6 — Scheduling, reliability, security, docs
- [x] Scheduler: enabled/due tasks, global + per-task locks, in-process thread option, systemd user timer generator (`Persistent=true`)
- [~] systemd timer installation — generated and documented, not installed on your machine
- [x] JSON export of all data; scoped deletion (with safety backup)
- [x] Security review (docs/SECURITY.md); URL sanitising; SSRF guard; CSP; CSV injection guard
- [x] Every page renders on an empty database; responsive layout checked at 1366 px and 390 px
- [x] Acceptance tests (tests/test_acceptance.py) covering all 13 acceptance criteria

## Known limitations
- Coverage is limited to the sources you configure; JavaScript-only career sites and login-walled boards aren't supported.
- Skill extraction is dictionary-based; unlisted skills aren't recognised (extend `services/skills_taxonomy.py`).
- Salary comparison doesn't convert currencies; mixed currencies are not compared.
- Interview times are stored as entered (local time + time-zone label), not converted.
- Gmail-imported interview suggestions match applications by company name in the email.
