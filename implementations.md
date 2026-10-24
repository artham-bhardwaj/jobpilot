# Implementations log

Every implementation in this repository, newest first, with a local timestamp. See `CLAUDE.md` for the rule and the
entry format. Earlier milestones (before this log existed) are summarised in `IMPLEMENTATION_STATUS.md`.

## 2026-10-24 22:16 IST — Dashboard built around applying yourself: apply panel, applied filter, Ready to apply

- Why: you apply only from your own UI, so applying and recording it now happen on one page instead of three, and the
  automation tools step back.
- **Job page:** new apply panel at the top: (1) resume to download (tailored PDF if valid, else the recommended
  version), (2) **Apply on company site ↗**, (3) **✓ I applied** with resume used (tailored option pre-selected when
  it exists) and date, plus Save for later / Not interested. Score shown in the title. Re-score, check still open,
  expire and triage moved under **More actions**. Description moved up, right after Relevance / Eligibility. The
  "Start application (prepare)" panel is now a secondary button with a note pointing to **I applied**.
- **Jobs list:** **Applied?** filter (any / not applied yet / applied), applied rows tinted with a "✓ applied <date>"
  badge and an **application** link; rows not applied get **apply ↗** (primary), **record / resume** (to the apply
  panel), save, dismiss. Seven rarely used filters folded under **More filters** (opens itself when one is in use).
  Heading "Job explorer" → "Jobs".
- **Overview:** **Ready to apply** card at the top: top 8 relevant, not ineligible, not applied jobs with apply and
  record buttons, and the total; header button **Jobs to apply to**.
- **Sidebar:** regrouped as Apply (Overview, Jobs, My tracker, Applications, Interviews), You, Assist & outreach, Setup.
- **Tracker route:** `/tracker/jobs/{id}/applied` accepts `resume_version_id=tailored` (records "tailored resume for
  this job (#id, from resume version #n)"; refused if none exists). Return path after tracker actions may now be `/`,
  `/tracker…` or `/jobs…` (local paths only; `//host` and absolute URLs fall back to `/tracker`).
- Files: `services/jobs.py` (`search(applied=…)`), `web/routes/jobs.py`, `web/routes/tracker.py`, `web/routes/core.py`,
  `web/templates/{base,dashboard,jobs,job_detail,_job_application}.html`, `web/static/app.css`, `howtouse.md`,
  `tests/test_m35_regions.py` (row button now links to `#apply`), new `tests/test_m36_apply_ui.py`.
- Migrations: none.
- Verification: full suite passed (`pytest -q`, all green) before and after; 4 new tests in `test_m36_apply_ui.py`
  pass. Live check: ran a second instance on port 8799 against a copy of the database (scheduler, Telegram and Gmail
  off), screenshotted Overview, Jobs and a job page at desktop and phone width with Chrome, and recorded "I applied"
  with the tailored option on the copy; it saved and the page showed "✓ Applied". The running service and real
  database were not touched.
- Manual action: `systemctl --user restart jobpilot` to load the new UI.

## 2026-10-24 00:05 IST — Auto-apply removed, resumes only on request, India / remote / Dubai job coverage, dashboard-first

- Why: you asked to drop automatic applications and automatic resume creation, fetch more jobs in India, remote and
  Dubai, and rely less on Telegram and more on the dashboard.
- **Auto-apply removed:** `services/autopilot.py`, `cli_autopilot.py` (`jobpilot autopilot …`), the `autopilot`
  scheduler task and setting, unattended submission (`apply.run_submission`, `rehearse_form`), Telegram `/batch`,
  `/exceptions` and batch buttons, the System-page autopilot panel, `autopilot_exception` review items and the
  `submission_batch` / `autopilot_exceptions` notifications. The browser helper (`jobpilot assist`) stays: it fills
  forms and you click Submit; its answer-bank switch moved to `browser.use_answer_bank`. Outreach keeps its own job
  filter (`outreach.min_score`, `allowed_eligibility`, `job_families`, same values as before) and its pause
  (`/pause`, System page). The `submission_batches` table is kept for history. Removed files (never committed to git)
  were archived first: `data/backups/removed-autopilot-code-20261023T232602.tar.gz`.
- **Resumes only on request:** the `tailor_resumes` task, `tailored.run` and the tailored-resume notifications are
  gone. **Create resume for this job** on the job page works for any job; low score, short description, expired or
  ineligible only add a note. The CLI no longer exits 2. Each Jobs row has a **resume** button.
- **Regions:** new `services/regions.py` tags each job `india` / `remote` (remote and open to India or the UAE) /
  `dubai` / `other`, stored in `jobs.regions`. Jobs page: region tabs with counts and badges. Dashboard: **Jobs by
  region** (count, new this week, top 5). Job page: badges. `/jobs` in Telegram shows the region. A remote job limited
  to another country gets a location warning in matching.
- **Sources:** board sources accept `"regions"` alongside `location_keywords` (Greenhouse, Lever, Ashby, Workday,
  Atlassian). New adapters `sources/remote.py`: Himalayas (search API, first page per query only per robots.txt) and
  Jobicy (credited; apply links go to Jobicy). New `services/regional_sources.py` presets with a **Sources → Add
  regional sources** button and `jobpilot regional-sources [--apply]`. Amazon's `ARE` country code added. Remotive
  (robots.txt disallows `/api/`) and Ashby boards such as Ziina (robots.txt disallows the job API) are not usable.
- **Telegram quieter:** off by default: `relevant_digest`, `draft_review`, `questions`, `recruiter_found`,
  `recruiter_needed`, `apply_check` (the dashboard shows them). Saved settings of removed features (scheduler tasks,
  notification kinds, `tailored_resumes.max_per_run`) are ignored on read.
- Files: `services/regions.py`, `services/regional_sources.py`, `sources/remote.py`,
  `migrations/versions/0013_job_regions.py`, `tests/apply_fixtures.py`, `tests/test_m35_regions.py` (new);
  `automation/apply.py`, `services/{jobs,matching,notifications,outreach_auto,queue,scheduler,settings_store,tailored,
  telegram_bot,telegram_ui,tracker,applications}.py`, `sources/{__init__,ats,careers,workday}.py`, `models/jobs.py`,
  `models/autopilot.py` (docstring), `cli_ext.py`, `cli_resume.py`, `cli_telegram.py`, `web/deps.py`,
  `web/routes/{core,jobs}.py`, `web/templates/{jobs,job_detail,dashboard,sources,system}.html`, `web/static/app.css`;
  tests m14, m22, m24–m29, m31–m34 updated (autopilot tests removed or rewritten for the assisted path); docs
  `howtouse.md`, `README.md`, `docs/{ARCHITECTURE,TELEGRAM,TAILORED_RESUMES,USER_GUIDE,OUTREACH}.md`; deleted
  `services/autopilot.py`, `cli_autopilot.py`, `tests/test_m18_autopilot.py`, `docs/AUTOPILOT.md`.
- Migration 0013 (`jobs.regions`, indexed; existing jobs back-filled) applied to the live DB (0012 → 0013) after the
  backup `data/backups/jobpilot-20261023T183223555075-before-regions-no-autoapply.zip`, with the service stopped.
- Live changes: the open autopilot exception (#6, Rubrik) was dismissed. 8 regional sources added (Himalayas, Jobicy,
  Bybit, Binance, OKX, Careem, Amazon UAE, Microsoft UAE). Regions added to 13 location-filtered board sources. A Ziina
  (Ashby) source was added, failed on robots.txt and was removed. A real run of the new sources fetched 44 new jobs
  (Himalayas 34, Jobicy 8, Binance 2; Bybit, OKX, Careem, Amazon UAE and Microsoft UAE had no matching titles,
  because their UAE engineering roles are senior or principal). All jobs were re-scored (241). Active jobs by region:
  India 208 (15 also remote), remote 46, Dubai / UAE 3. The service was restarted; pages return 200.
- Verification: full suite **806 passed, 0 failed** (before removing Ziina); `tests/test_m35_regions.py` 31 passed
  after it. Adapters were tested with canned HTTP and checked live against Himalayas, Jobicy, Greenhouse boards,
  amazon.jobs and Microsoft careers.
- Open: Dubai coverage is thin. Few UAE companies publish entry or mid-level roles on boards whose robots.txt allows
  reading (Bayt, GulfTalent and LinkedIn are not used). Add a Dubai company's Greenhouse or Lever board on Sources if
  you find one. Your profile's acceptable locations don't list Dubai; Dubai jobs still pass because you're willing to
  relocate.

## 2026-10-18 22:32 IST — Master resume v2 (Class XII 2020), batching fix, outdated tailored resumes, howtouse.md

- Backup first: `data/backups/jobpilot-20261018T165123777039-before-master-v2.zip`.
- v2 source: `data/files/resume_sources/ArthamBhardwaj_Master_v2.tex` (read-only, sha256 52911e18…e3aa) = the
  registered v1 template with one line changed (`\textit{2021}` → `\textit{2020}` on the Class XII entry; `diff`
  shows only that line). Compiled locally (deterministic): 2 A4 pages; text diff with the approved v1 PDF: only
  "2021" → "2020"; letters/digits differ by exactly 1→0; page 1 pixel-identical to v1 compiled from source, page 2
  differs in 27 pixels in one 4×7 spot (the digit); no placeholders or errors; page 2 rendered and inspected.
- Approval through the existing workflow: `resumes.import_version` → Master resume (#1) version 2 (draft,
  `resumes/9824ec0a37a33a7e_ArthamBhardwaj_Master_v2.pdf`, sha256 9824ec0a…7d14, no validation errors) →
  `review_version` flagged "material" (43 lines, 11 skills "added") — checked: caused by v1's stored text having
  different spacing and v1's skills list being computed with an older skill dictionary (both texts give the same
  skills today; content differs only 1→0) → `approve_version` without acknowledgement refused; with acknowledgement
  approved 2026-10-18 17:00:19 UTC, audit #272 (`acknowledged: true`), approval review stored on the version. The
  active Master version is v2; v1 stays approved.
- Template: `jobpilot resume template --file …v2.tex` registered `data/files/templates/master_v2_52911e1833f9.tex`
  (source version #2); the v1 source is now refused (exit 3, text differs). 6 tailored resumes made from v1 (#2–#7)
  marked `outdated` (new behaviour of `register_template`; never attached; files and records kept).
- Bug fixed (`tailored.run`): each run took the first `max_per_run` eligible jobs even when their resumes were current
  ("reused"), so later jobs were never reached; up-to-date jobs are now skipped before the batch is taken, and the
  result reports `up_to_date` and `remaining`. `master_version` now queries the approved Master version directly.
- Live: job #126 regenerated from v2 → tailored resume #8 (valid, all checks, no corrections needed; same file as #7
  because the content is identical). The rest (Amazon 20, 69, 100, 18, 21 and the other eligible jobs) are left to
  the `tailor_resumes` batches (5 per 6 h). Applications #1–#3 and approved batch #1 still reference v1 (approvals
  unchanged); autopilot settings unchanged. Service restarted.
- Hashes after: v1 PDF 8da104a9…b2eb, `~/Downloads/twopage.tex` 96831601…a9a9, v1 template copy 96831601…a9a9 — all
  unchanged; v2 PDF 9824ec0a…7d14, v2 source and template copy 52911e18…e3aa.
- Docs: `howtouse.md` (new, complete usage guide; linked from README), `docs/TAILORED_RESUMES.md` ("A new Master
  version").
- Files: `services/tailored.py`, `models/tailored.py`, `web/routes/jobs.py`, `web/templates/job_detail.html`,
  `tests/test_m34_tailored.py` (+2 tests: batches skip current resumes; a new Master version outdates tailored
  resumes and keeps v1 for its applications), `README.md`, `howtouse.md`, `docs/TAILORED_RESUMES.md`.
- Verification: tailored tests 29 passed; full suite: 811 passed, 0 failed. No application submitted, no email sent.
- Open: application #1 (Rubrik, preparing, not approved) still uses v1; switch it to v2 on its page if you want.

## 2026-10-18 22:02 IST — Class XII year: tailored resumes follow the confirmed education record (2020)

- Cause: education #2 (CBSE, Class XII) was imported from the Master resume on 2026-10-17 with the note "Dates on
  resume: 2021 (year only)"; you then edited it on the dashboard to 2019-04-10 → 2020-05-12 and confirmed it. The
  record was right (2020); the approved Master resume (PDF and LaTeX source) says 2021, and tailored resumes copied
  the template's 2021 with a warning.
- Record changed (backup first: `data/backups/jobpilot-20261018T162441788757-before-class12-year.zip`): education #2
  `notes` only — the stale "Dates on resume: 2021 (year only)" line replaced by "Passed in 2020 (confirmed by you on
  2026-10-18). The approved Master resume v1 shows 2021, which is incorrect."; the CBSE note kept. Dates, grade,
  status, source and confirmation unchanged (via `profile.save_fact`, audited).
- Generator: education entries' years now come from the confirmed record (single year = completion year; range =
  start/end years), recorded in `decisions.corrections`; validation's content comparison allows exactly those
  corrections. Unconfirmed records never change anything; entries without confirmed dates are copied with a note.
  The year-conflict warning is replaced by the correction. `PIPELINE_VERSION` 1 → 2, so every earlier PDF is stale
  (never attached) and is superseded on regeneration.
- Not modified: Master PDF (sha256 8da104a9…), `~/Downloads/twopage.tex` and the registered template copy
  (sha256 96831601…) — hashes checked after regeneration.
- Live: job #126 regenerated through the versioning workflow → tailored resume #7
  (`tailored_resumes/google_126_022bb041c617.pdf`, 2 pages, all 13 checks passed, "Class XII (Science) — CBSE 2020");
  #1 (2021) superseded. Diff vs the Master: only the DevOps skill order and 2021 → 2020. Service restarted.
- Still showing 2021, stale, never attached to any application: #2–#6 (Amazon jobs 20, 69, 100, 18, 21). They are
  the first five the next scheduled `tailor_resumes` run processes (max 5 per run), which supersedes and regenerates
  them; until then the approved resume would be attached instead.
- Files: `services/tailored.py`, `docs/TAILORED_RESUMES.md`, `tests/test_m34_tailored.py` (+5 tests).
- Verification: tailored tests 27 passed (5 new: record year replaces the template's, unconfirmed record ignored,
  date ranges, real compile showing 2020 + a PDF still showing 2021 rejected, stale artifact superseded and kept);
  full suite: 809 passed, 0 failed.
- For you: the approved Master resume v1 itself still says 2021 (not modified, as instructed); it is what gets attached
  wherever no current tailored resume exists. To fix it, correct the LaTeX source, approve the new PDF as a new Master
  version and register its source.

## 2026-10-18 21:37 IST — Tailored resumes per job from the approved Master resume's LaTeX source

- Audit: Master resume v1 (approved) is an imported PDF (pdfTeX, 2 pages); JobPilot had no LaTeX source and no
  compiler. The source was found outside the repo (`~/Downloads/twopage.tex`): its text is identical to the approved
  PDF (pdftotext layout diff empty; letter/digit multiset equal) and a local compile renders the same 2 A4 pages
  (≤ 0.02 % anti-aliasing pixel differences from a newer TeX Live). All profile facts used are confirmed. TeX Live
  installed by you (`sudo apt install latexmk texlive-latex-recommended texlive-fonts-recommended`).
- New `services/tailored.py`: lossless LaTeX parser (sections + nested itemize; byte-identical round trip), mapping of
  every line to a confirmed fact (employer/title/dates must equal the confirmed employment record; bullets must equal
  confirmed description lines; unconfirmed/unmatched content left out), tailoring = re-ordering only (bullets,
  projects, skills within a line, by the job's required/preferred/mentioned skills), local compile (latexmk/pdflatex,
  `-no-shell-escape`, timeout, temp dir, fixed SOURCE_DATE_EPOCH → deterministic bytes), 13 validation checks
  (incl. same text as the Master minus exclusions, every line present, no placeholders/LaTeX errors/blank pages, page
  target, words inside the page, Master unchanged), versioned storage `data/files/tailored_resumes/<company>_<job>_
  <hash>.pdf`, reuse on unchanged inputs, superseding on changes, eligibility = existing rules.
- Attachment: `tailored.attachment_for` now chooses the upload in assisted filling and autopilot submissions: the
  job's valid, current tailored resume (same job/company, validated, file hash, made from the application's resume,
  template/job/facts unchanged), else the checksum-verified approved resume. Unattended submissions use it only if it
  was validated before you approved the submission. The challenge exception's next action now includes the
  application link and says when the tailored resume will be attached.
- CLI `jobpilot resume template|preview|generate|regenerate|validate|list|show|path` (exit codes 0/1/2/3/4; backup
  before first generation per job and before regeneration); dashboard panel on the job page (status, decisions,
  mapping, download, generate); notifications `tailored_resume` / `tailored_failed` (deduplicated); scheduler task
  `tailor_resumes` (default off, runs before autopilot); settings `tailored_resumes`.
- Files: `services/tailored.py`, `models/tailored.py`, `migrations/versions/0012_tailored_resumes.py`,
  `cli_resume.py` (new); `models/__init__.py`, `cli_ext.py`, `automation/apply.py`, `services/autopilot.py`,
  `services/scheduler.py`, `services/settings_store.py`, `services/notifications.py`, `services/telegram_ui.py`,
  `web/routes/jobs.py`, `web/templates/job_detail.html`, `web/static/app.css`; docs `docs/TAILORED_RESUMES.md` (new),
  `docs/ARCHITECTURE.md`, `docs/USER_GUIDE.md`, `docs/TELEGRAM.md`; tests `tests/test_m34_tailored.py` (new, 22).
- Migration 0012 (`tailored_resumes`) applied to the live DB (revision 0011 → 0012) after an explicit backup
  `data/backups/jobpilot-20261018T155102778618-before-tailored-resumes.zip` (plus migrate's own pre-migrate backup).
- Live rollout: template registered (`data/files/templates/master_v1_968316019d5d.tex`, read-only, sha256 9683…a9a9,
  source version #1). Job #126 (Google, Software Engineer II, Google Cloud): preview → 53 lines mapped, none excluded,
  not claimed: c++, gcp, go; generated `tailored_resumes/google_126_51ca166ca532.pdf` (2 pages, sha256 407e…abdb),
  all 13 checks passed; only difference from the Master (pdftotext diff): "Kubernetes, Docker" instead of "Docker,
  Kubernetes" in DevOps & Observability; page 1 rendered and inspected. Re-run reused record #1 (one file); `validate`
  passed; ineligible job #178 → exit 2; unknown job → exit 1. Master PDF sha256 8da104…b2eb unchanged before/after.
- Verification: `tests/test_m34_tailored.py` 22 passed (real local compiles; failure paths with a fake compiler);
  autopilot + assisted suites 59 passed; full suite before the unattended rule: 803 passed, 0 failed; full suite after:
  804 passed, 0 failed. Scheduler task `tailor_resumes` then enabled (every 6 h, 5 jobs per run;
  33 eligible jobs) and the service restarted. First scheduled run (16:13 UTC): "5 tailored resume(s) generated, 0 reused,
  0 failed" — Amazon #20, #69, #100, #18, #21, all valid (2 pages, all checks passed); #18/#20/#21 needed no re-ordering
  (content hash equals the template's). Master PDF hash unchanged afterwards. Failure handling was exercised by tests only (mocked compiler missing / compile error / invalid
  PDFs), not on the live database.
- Open for you: Class XII year differs (resume "2021", profile record end 2020-05-12) — copied unchanged, flagged
  in every generation's warnings; decide which is right. The template has no summary section, so none is added.
  Wording is never changed (re-ordering only). Tailoring for application #2 (Rubrik, approved batch) will not replace
  the resume you approved for its unattended submission.

## 2026-10-18 20:03 IST — Application tracker: did you apply + which resume, company search and cooldowns, dream companies, employer-email outcomes, Apple source

- New `services/tracker.py`: record an application you made yourself (confirmation method `self_reported`, resume
  version or "another resume", date), "not yet" (asked again after `ask_again_days`) and "not interested" (dismisses
  the job); applications grouped by company with last application date and a per-company cooldown you set (none is
  assumed); dream companies (default Meta/Facebook, Apple, Amazon, Netflix, Google/Alphabet; whole-word match, so
  "Amazon Web Services" counts) listed first.
- Employer emails (`detect_outcome_emails`, run by the `gmail_sync` task after the confirmation detector; headers
  only, metadata scope): for applied/assessment/interview applications, a subject about an application that names
  the company → unambiguous rejection/assessment/interview/offer with one matching application updates the status
  (event actor `system`, Undo); anything unclear → review item `email_outcome` with buttons. Confirmation mail and
  non-application mail (orders etc.) ignored; each message looked at once.
- Telegram: `/alljobs`, `/track`, `/applied`, `/company <name>`, `/cooldown <company> <days|off>`, `/dream`; buttons
  `t:a` (I applied → resume picker `t:r:<job>:<version|0>`), `t:n`, `t:x`, `o:<item>:<status>`, `u:<app>`; new
  notification kinds `apply_check` (≤ `ask_daily`=5 per local day, jobs found ≥ 24 h ago) and `email_outcome`
  (automatic updates carry Undo and are no longer repeated in the status digest); high-match alerts get "✅ I applied".
  Menu and persistent keyboard extended; 20 bot commands registered.
- Dashboard: new **My tracker** page `/tracker` (all jobs with apply links, search by company/title, include-ineligible
  toggle, applied form with resume + date (future dates refused), not yet / not interested, applications by company
  with resume, cooldown status and undo, employer-email questions, cooldown form).
- Settings: new `tracker` section (`TrackerSettings`: dream_companies, cooldown_days 1–730, ask_daily, ask_again_days,
  auto_outcomes); notification kinds `apply_check`, `email_outcome`.
- New source adapter `apple_jobs` (jobs.apple.com search + job details from the page's embedded data; robots.txt
  answers 404 = no restrictions; postings of one requisition per city merged into one job; fails loudly on a changed
  layout). Meta not covered (terms forbid automated collection); Netflix had no India roles.
- Files: `services/tracker.py` (new), `web/routes/tracker.py` (new), `web/templates/tracker.html` (new),
  `services/settings_store.py`, `services/notifications.py`, `services/scheduler.py`, `services/telegram_bot.py`,
  `services/telegram_ui.py`, `cli_telegram.py`, `cli.py` (start-up message), `sources/careers.py`,
  `sources/__init__.py`, `web/routes/__init__.py`, `web/templates/base.html`, `docs/TELEGRAM.md`, `docs/USER_GUIDE.md`,
  tests `tests/test_m33_tracker.py` (new), `tests/test_m27_automation.py` and `tests/test_m32_telegram_ui.py`
  (expectations updated for the extra Gmail-sync message and the new keyboard row).
- Migrations: none (state in `app_settings`, `review_items`, application events/evidence).
- Live changes (backup first: `data/backups/jobpilot-20261018T142938688572-before-tracker.zip`): source #18
  "Apple (jobs.apple.com)" added; dry run then real run: 120 postings listed, 11 matched, 11 new. Jobs #170 and #177
  were the Hyderabad postings of the same Apple requisitions as #169 / #178 (found before the merge fix): marked
  expired with a note and an audit entry (`job.expired_duplicate`), not deleted. Service restarted; `/tracker` → 200
  on the live server; `jobpilot telegram menu` registered 20 commands and sent the keyboard + menu (Telegram message
  ids 63, 64).
- Verification: `tests/test_m33_tracker.py` 10 passed (fake Telegram updates at handler level, fake Gmail inbox,
  saved-page Apple fixtures — all mocked); full suite: 782 passed, 0 failed. Live, read-only: tracker page rendered
  against the real database (34 relevant jobs, 5 for "google", 174 with ineligible included).
- Not done / for you: no "Did you apply?" prompts go out today because every relevant job was found less than 24 h ago
  (use `/track` now; scheduled prompts start tomorrow). No cooldowns set (none assumed). Live employer-email
  detection runs at the next `gmail_sync` (6-hourly); nothing has been classified from your real inbox yet. The
  title "Computer Scientist 2" on Adobe job #165 comes from the source while its URL says SDE 4 — check it before
  applying.

## 2026-10-18 18:14 IST — Telegram: formatted messages and buttons everywhere

- New `services/telegram_ui.py`: HTML formatting applied to every bot message and notification (all content escaped
  first; bold heading, bold "Label:", `code` spans), plain-text fallback if Telegram rejects the formatting.
- Buttons: `/menu` (and test/daily summary) show a button menu (Status, Today, Jobs, Applications, Questions,
  Exceptions, Batches, Outcomes, Recruiters, Outreach, Pause, Resume); `/start` sets a persistent keyboard; every
  notification kind gets related buttons (job alerts: "🔗 Open posting" for https links; questions: "✍️ Answer Q12";
  failures: Status …); command replies get context buttons (/jobs: top non-ineligible postings). "✍️ Answer" opens
  Telegram's reply box and your reply answers that question through the same checks as /answer (legal items get no
  button and are refused). Batch decision buttons unchanged. `jobpilot telegram menu` registers the "/" command list
  (`setMyCommands`) and sends the keyboard and menu.
- Files: `services/{telegram_ui (new),telegram_bot,notifications}.py`, `integrations/telegram.py`, `cli_telegram.py`,
  `tests/test_m32_telegram_ui.py` (new, 8), `tests/test_m14_telegram.py` (fake shows text as read; hint wording).
- Migrations: none. Verification: full suite 772 passed, 0 failed, 0 skipped. Live: service restarted; `jobpilot
  telegram menu` → 15 commands registered, keyboard + menu delivered (message ids 53, 54); doctor all ok.

## 2026-10-18 16:48 IST — Activation: autopilot autonomous, automatic recruiter outreach, daily briefing

- New `services/outreach_auto.py` + scheduler task `outreach` (every 6 h, after autopilot, before notify): for jobs that
  pass the autopilot policy, one recruiter email per job; no contact → `recruiter_needed` queue; address not verified
  by you → `recruiter_unverified`; facts missing / domain mismatch → `outreach_review`; verified + `auto_send` →
  approved under standing authorization and sent through the existing Gmail send path (verified recipient, not
  blocked/bounced, daily limits, no retry on unknown outcome), max `auto_daily_limit` (3)/day; failures →
  `outreach_failed`, never marked sent; old/cancelled drafts never sent; paused → nothing.
- Telegram: "🔍 Recruiter outreach needs you" alerts; "📧 Recruiter email SENT" only with a Gmail message id; daily
  briefing adds blocked applications, questions/legal items, emails sent, recruiter tasks, interviews in 7 days, and
  "strong matches" now excludes ineligible jobs.
- Fixed (caught by an existing test): the scheduler settings schema did not accept the new `outreach` task, which would
  have made saving scheduler settings fail.
- Files: `services/{outreach_auto (new),scheduler,settings_store,notifications}.py`, `tests/test_m31_outreach_auto.py`
  (new, 10), `tests/test_m14_telegram.py` (briefing wording), `docs/OUTREACH.md`.
- Migrations: none. Backup `pre-activation` verified. Verification: full suite 764 passed, 0 failed, 0 skipped
  (a first run showed the schema failure above; fixed and re-run).
- Activated (your explicit authorization): `autopilot.submission_mode` autonomous, batch approval required, daily
  limit 3; scheduler `autopilot` hourly and `outreach` every 6 h; `outreach.auto_enabled` + `auto_send`, 3/day,
  verified recipients only. Live: scheduler ran autopilot (prepared application #2 for Rubrik #131, batch #1) and
  outreach (4 jobs queued for a recruiter) at 11:07 UTC; Telegram test message delivered (id 40); batch, application
  and recruiter alerts delivered (ids 41–43); you approved batch #1 in Telegram at 11:18 UTC (verified live callback).
  No application submitted, no email sent, no Apollo call. Doctor all ok; integrity ok; profile/TCS unchanged.

## 2026-10-18 15:28 IST — Cleanup commands for placeholder contacts and old drafts

- Found: the dashboard could cancel drafts (soft, audited) and delete contacts, but the contact delete was a bare row
  delete with no record of what was removed and no check for drafts/interactions; the CLI had neither.
- Added `contacts.delete_contact` (refuses when any draft or interaction refers to the contact, requires a reason,
  removes its job links, audit entry records what was removed) used by both the dashboard and
  `jobpilot recruiters remove --id N --reason "…" [--yes]` (without `--yes`: shows what would be removed);
  `jobpilot recruiters update --id N …` (correct in place, unchanged fields kept); `jobpilot outreach cancel --draft N`
  (existing soft cancel: kept with history, can no longer be approved, put in Gmail or sent).
- Files: `services/contacts.py`, `cli_recruiters.py`, `web/routes/outreach.py`, `tests/test_m30_cleanup.py` (new, 5).
- Migrations: none. Verification: focused 110 passed; full suite 754 passed, 0 failed, 0 skipped. Live: read-only dry
  run of removing contact 1 only; contact 1 and draft 1 unchanged (left for you to run). Nothing sent or created.

## 2026-10-18 15:13 IST — Recruiter outreach, Apollo (manual + optional API), legal boundary, six-way form report

- Reused the existing contacts / drafts / do-not-contact / Gmail-draft architecture. Added: contact sources
  `linkedin_manual`, `apollo_manual`, `apollo_api`; email statuses `apollo_reported` (Apollo's claim, not confirmed)
  and `bounced` (blocked everywhere via `is_blocked`); company/domain warnings (never silent changes); personal-domain
  emails not stored from imports.
- New `services/apollo.py`: Apollo CSV import (manual mode); optional API (off unless `apollo.enabled` and
  `JOBPILOT_APOLLO_API_KEY`): People API Search (0 credits, nothing stored) and People Enrichment for one person only
  with `--spend-credit` and within `apollo.max_enrichments_per_day` (default 0); personal emails/phones never requested.
- New `cli_recruiters.py`: `jobpilot recruiters add|import|list|search|enrich`, `jobpilot outreach draft|approve|list`
  (Gmail via the existing `jobpilot gmail create-draft`, never sends).
- Outreach drafts: one per recruiter per job; a second recruiter for a job only with a reason; the recruiter template
  now uses your confirmed current role and years and at most one confirmed personal project that uses a skill the job
  asks for; an unedited stored built-in template is refreshed, edited ones are kept.
- Fixed: a Gmail delivery-failure notice in a sent thread counted as a recruiter reply; it is now a bounce (address
  blocked, follow-ups stop).
- Fixed: legal attestations (acknowledgements, consent, NDA, signatures, "type your full name") could be filled from an
  approved answer and were invited via `/answer`; now never filled, `/answer` refuses them, alerts say "complete it
  yourself on the form". Questions the profile has filled and verified on the form are closed as `resolved` (not
  approved); live: application #1 Q1 (Country) and Q2 (City) resolved, Q3–Q5 stay open.
- Form report now six sections: filled and verified / required missing / awaiting your answer / legal or consent
  decision / optional blank / not identified or verified.
- Telegram: `/recruiters`, `/outreach`, `/outreach_pending`, `/outreach_stats`; `/jobs` shows hard conflicts, existing
  applications, platform limits and the direct apply URL; new alerts: questions waiting, recruiter contacts added,
  recruiter emails in Gmail Drafts (not sent), bounces. Dashboard contact form/detail show the new sources, statuses
  and domain warnings.
- Files: `services/{apollo (new),contacts,drafts,answers,notifications,telegram_bot,settings_store,autopilot,
  applications}.py`, `integrations/gmail.py`, `automation/apply.py`, `config.py`, `cli_recruiters.py` (new),
  `cli_ext.py`, `cli.py`, `web/routes/outreach.py`, `web/templates/{_contact_form,contact_detail}.html`,
  `web/static/app.css`, `tests/test_m29_outreach.py` (new, 24), `tests/test_m28_form_filling.py`,
  `tests/test_m25_telegram_replies.py`, `docs/{OUTREACH (new),TELEGRAM}.md`.
- Migrations: none (head 0011; new values fit existing string columns). Backup `pre-m29-outreach` verified.
- Verification: baseline 724 passed; final full suite 749 passed, 0 failed, 0 skipped. doctor all ok; integrity ok;
  application, profile, TCS, resume and job decisions unchanged; service stopped ~10 min during the legal-boundary fix
  (before any stale question alert was sent), restarted. Mode manual, batch approval required, limit 3, autopilot task
  disabled. No application submitted, no email sent, no Apollo call made (no key configured).
- Remaining: no recruiter contacts yet; draft #1 (to recruiter@mastercard.com, no contact record) is unverified and
  unlinked; reply classification beyond "reply received" needs message bodies (disabled).

## 2026-10-18 14:07 IST — Assisted form filling: complete, verified, reported

- Root causes found on the live Rubrik form: (1) drop-down options were read from every `role=option` on the page,
  so the city search read the phone field's always-present country-code list; (2) country options carry dial codes
  ("India +91") and the picker then shows only "+91"; (3) the profile has no country field; (4) city options are
  "Thane, Maharashtra, India", never equal to "Thane"; (5) nothing read values back, and focus was left in the last
  field filled, so a later change (employer "…ServicesS"; stored value is correct) went unnoticed; (6) option
  markers were reused across drop-downs; (7) checkboxes looked "already answered" (value "on").
- Fixed: options scoped to the drop-down's own list (aria-controls/aria-owns, else its own container) with unique
  markers; exact matching ignoring only a trailing dial code; country derived only from a stated or unambiguous
  profile location; a city option is chosen only if its first part is exactly your city and it names your country;
  values validated before entry (employer must equal the confirmed current role); every value read back after
  filling (phone by digits), one retry, a wrong value cleared and reported; focus released before handover;
  checkbox state from `checked`; contenteditable fields; re-scan for fields rendered after answers; labels that match
  several profile fields reported, not guessed; `jobpilot assist` waits for the form to render and prints a
  completion report (filled and verified / required missing / your decision / optional blank / not
  identified-verified). Legal acknowledgements, NDA signatures, consent and unanswered questions stay yours.
- Files: `automation/apply.py`, `cli_ext.py`, `tests/test_m28_form_filling.py` (new, 14), `tests/test_stage4.py`
  (optional demographic question now reported as optional, not blocking).
- Migrations: none. Verification: full suite 724 passed, 0 failed, 0 skipped. Live Rubrik form (sealed after load;
  only Greenhouse's city-lookup GETs allowed; no POST left the browser; nothing clicked): 10 fields filled and
  verified (incl. Country India/+91, City "Thane, Maharashtra, India", employer exactly "Tata Consultancy
  Services"); none required missing; 3 for you (privacy notice, NDA, prior Rubrik employment); cover letter optional.
  doctor all ok; mode manual.

## 2026-10-18 13:23 IST — Final automation configuration (kept manual; two fixes)

- Fixed: the Gmail confirmation detector counted any browser run as "form opened", including runs refused before
  opening (site not approved, browser disabled); only runs that showed the form count now.
- Fixed: `/answer … +bank` created a new answer-bank entry each time; it now updates the entry for that question.
- Decision (per your policy): autonomous mode and the autopilot task stay off. Simulation on a copy of the database
  showed an autonomous run would prepare Rubrik #131, send one batch, and after approval open its real form once,
  end "blocked" (invisible bot check) and not retry; the real form (read-only rehearsal) also has 10 required
  fields with no approved answer and 2 legal acknowledgements. No allowed platform supports safe unattended
  submission. The invisible-check opt-in was not implemented.
- Files: `integrations/gmail.py`, `services/telegram_bot.py`, `tests/test_m27_automation.py` (+2).
- Migrations: none. Verification: full suite 710 passed, 0 failed, 0 skipped; doctor all ok; autopilot status manual /
  required / limit 3 / 0 exceptions; preview 1 pass, 2 blocked by Google sign-in; scheduler: 9 maintenance tasks
  enabled, autopilot disabled. Settings, thresholds, profile, resume and application #1 unchanged.

## 2026-10-18 13:14 IST — Less manual work: Telegram answers, confirmation emails

- Telegram `/questions` and `/answer Q<id> <text> [+bank]`: answer application questions from your phone; the
  answer is approved as yours (sensitive ones for that application only; `+bank` saves non-sensitive answers to the
  approved bank for later forms). The last answer closes the "answer missing" exception so the next run continues.
  Exception alerts now list the open questions with their numbers.
- Gmail: `detect_confirmation_emails` (in the `gmail_sync` task) reads only the headers of recent inbox messages
  (existing compose + metadata scopes; no search, no bodies) and records a submission when the employer's
  confirmation email arrives for an application whose form JobPilot opened (strict subject, sender-domain, company,
  time and single-match rules); exceptions close and follow-ups start. Each message is examined once.
- Not done: an opt-in to let unattended submissions proceed past Greenhouse's invisible bot check was blocked by
  the session's permission policy and is not implemented.
- Files: `services/{telegram_bot,autopilot,applications,scheduler}.py`, `integrations/gmail.py`, `cli.py`,
  `tests/test_m27_automation.py` (new, 5), `tests/test_m24_assisted.py` (wording), `docs/{TELEGRAM,AUTOPILOT}.md`.
- Migrations: none. Mode manual, autopilot scheduler disabled, batch approval required; thresholds, decisions and
  profile unchanged.
- Verification: full suite 708 passed, 0 failed, 0 skipped. Live: service restarted; detector found no opened,
  unrecorded application (no Gmail call made); `/questions` answers correctly. Nothing sent or submitted.

## 2026-10-18 12:38 IST — End-to-end audit and lifecycle hardening

- Audit (code as source of truth): lifecycle states reused (discovered → shortlisted → preparing →
  ready_for_review [+ submission_approved_at] → applied → assessment/interview/offer/rejected, withdrawn; browser-run
  states paused/failed/submitting/uncertain/confirmed), transitions validated in `applications.transition`,
  `applied` only via evidence + attestation or a verified confirmation page.
- Fixed: the browser paths attached the resume after checking only that the file existed; now
  `resumes.verified_file` requires the approved version and a matching sha256 (pre-submit check, unattended
  submission, `jobpilot assist`, rehearsal). A changed file is never uploaded.
- Fixed: a batch approval never lapsed, so `/resume` after a long pause would submit on an old decision. Approvals
  unused after `batch_expiry_hours` now return the application to preparing (event "authorization expired") and it
  is offered in a new batch with a new one-time code.
- Fixed: applications from an expired batch were never offered again although the reply promised it.
- Added: radio-button groups are filled only when exactly one option equals the approved answer (sensitive ones
  only from that application's own approved answers); checkboxes stay untouched. `jobpilot assist` on an unsupported
  platform now states the platform's requirement and the URL to complete manually.
- Files: `services/{autopilot,resumes}.py`, `automation/apply.py`, `tests/test_m26_lifecycle.py` (new, 12),
  `docs/AUTOPILOT.md`.
- Migrations: none (head 0011). Backup `pre-m26-final` created and verified (readable, includes the DB and resume).
- Verification: baseline 691 passed; final full suite 703 passed, 0 failed, 0 skipped. Live: `jobpilot collect`
  16/16 sources ok, 155 fetched, 0 new, 155 unchanged, 0 duplicates; `jobpilot rematch` 165; preview 1 pass
  (Rubrik #131), 2 blocked by Google sign-in; no job decision, note, status or application changed; integrity ok;
  notify dry run sends nothing; state identical after two service restarts; Telegram getMe ok; Gmail scopes
  compose + metadata only. No application, email or batch approval; no new Gmail draft.
- Remaining: every real platform needs a human step (bot check or login); answer bank has no approved entries.

## 2026-10-18 12:17 IST — High-match alerts: no alerts for ineligible jobs

- High-match alerts now go only to eligible / likely-eligible jobs and to jobs with unknown eligibility (headed
  "eligibility unknown: review the requirements before applying"). Ineligible (or hard-conflict) jobs go to the
  existing digest, marked "⛔ ineligible". An ineligible job whose high-match alert was already delivered is not
  announced again in the digest; a job that later becomes eligible still gets its high-match alert.
- Files: `services/notifications.py` (`collect_events`), `tests/test_m25_telegram_replies.py` (+3 tests).
- Migrations: none. Scores, eligibility, thresholds, collection, policy and delivery/dedup unchanged.
- Verification: full suite 691 passed, 0 failed, 0 skipped. Live dry run (`jobpilot telegram notify --dry-run` plus
  the event keys): nothing new would be sent; no earlier alert repeats. Service restarted; submission_mode manual,
  autopilot scheduler disabled.

## 2026-10-18 11:55 IST — Real Telegram integration validation

- Validated the live bot: token check (`getMe`), configured id is a private chat with you (`getChat`; so it is also
  your user id), no webhook, service running the latest code. The command handlers the poller uses were run against
  the live database for /status, /jobs, /pending, /exceptions, /outcomes, /help: answers match the database and
  contain no token, OAuth token, client secret, phone number or resume text. /pause then /resume run once (manual
  mode; policy, browser runs and application #1 unchanged).
- Delivered to your chat only: `jobpilot telegram test`, one labelled validation message with those replies, and a
  test button (`t:ping`, tied to nothing). You pressed it: the running service recorded an authorized callback from
  your chat and answered "not recognised, nothing changed".
- Improved: /jobs shows eligibility per job (✅ / ❓ / ⛔) with a note that a score is relevance, not eligibility;
  /status prints notification counts readably.
- Files: `services/telegram_bot.py`, `tests/test_m25_telegram_replies.py` (new, 4).
- Migrations: none. submission_mode manual, autopilot scheduler disabled, thresholds, decisions, TCS dates and the
  1.0-year total unchanged.
- Verification: full suite 688 passed, 0 failed, 0 skipped. Backup `pre-m25-telegram` before pause/resume. All 27
  stored notifications are `sent` with Telegram message ids; no secret in data/logs or today's service journal.
- Remaining: a real submission batch's Approve/Skip buttons were not pressed (no batch can exist in manual mode);
  "High match" alerts are also sent for ineligible (non-hard-conflict) jobs, with the eligibility shown.

## 2026-10-18 11:40 IST — Telegram → assisted application workflow

- Telegram now says what to do next for every blocked application: `autopilot.next_action()` (one place) is used by
  the grouped 🚧 alert and `/exceptions`; for a challenge it names the kind (CAPTCHA, invisible bot check, login,
  MFA) and gives the exact `jobpilot assist <id> --approve-host <host>` command. Batch messages list what each
  unsupported platform needs.
- Fixed (found on the live Rubrik form): assisted mode filled nothing on any page with a bot check, i.e. every
  Greenhouse form. `assist_page(stop_on_blockers=False)` for `run_assist` fills around the check and reports it;
  the unattended path still stops before touching anything.
- Fixed: the screenshot kept after an *uncertain* outcome was stored as confirmation-grade evidence ("screenshot");
  it is now a plain "file", so it can never satisfy the evidence check for recording a submission.
- Files: `services/{autopilot,notifications,telegram_bot}.py`, `automation/apply.py`, `tests/test_m24_assisted.py`
  (new, 16), `docs/AUTOPILOT.md`.
- Migrations: none. submission_mode manual, autopilot scheduler disabled, batch approval required; thresholds and
  experience unchanged (TCS 2025-12-26 → present, total 1.0).
- Verification: full suite 684 passed, 0 failed, 0 skipped. Live: backup `pre-m24-assisted`; application #1
  prepared for Rubrik #130 (status preparing; nothing approved or submitted); assisted fill validated on the real
  Rubrik Greenhouse form with the browser sealed after load (6 outgoing requests aborted, incl. Greenhouse's
  resume upload to S3): 8 fields + resume filled, 5 questions left for you, submit not clicked. Gmail: labelled test
  draft #3 to your own address with the approved resume attached (81,578 bytes, sha256 matches); Gmail shows label
  DRAFT only; not sent.
- Remaining / for you: delete the Gmail test draft after checking it; complete application #1 yourself with
  `jobpilot assist 1 --approve-host job-boards.greenhouse.io` if you want to apply; Telegram callbacks were tested
  with a fake Telegram only.

## 2026-10-18 11:20 IST — Job pipeline quality: Workday descriptions and search, classification, scoring, parser

- Workday JobPosting descriptions arrive flattened to one line, so every Workday posting looked entirely
  "preferred" (14 of 14 affected). Flattened Workday postings now take their HTML description from the same site's
  job API (robots.txt allows it); the JobPosting text is kept on any failure.
- New `workday` source kind: the site's own job search (JSON POST, new `PoliteHttp.post_json` with the same
  robots/rate-limit/size/SSRF checks), then each kept posting's details, including the official closing date. The
  Visa, Mastercard and Adobe sources (ids 2–4) were switched from `sitemap` to `workday`; the old config is in each
  source's notes.
- Classification: SDE I (a level) now needs a title match (`title_required`); description skills alone made senior
  Adobe roles "SDE I". "Software Dev Engineer" and "SDE" titles now classify as General SWE (#75 had no family).
- Scoring: an unstated work mode no longer costs location points when every work mode is in your preferences; it
  is still reported as missing job information, and still partial credit if you exclude a mode.
- Parser: the verb "master" ("eagerness to master a front end stack") is no longer a Master's-degree requirement;
  "learn and use / you will learn / opportunity to learn" mark skills as preferred, not required.
- Files: `sources/workday.py` (new), `sources/{pages,http,__init__}.py`, `services/{matching,settings_store,
  job_parser}.py`, `tests/helpers.py` (fake POST), `tests/test_m23_pipeline.py` (new, 27), `docs/USER_GUIDE.md`.
- Migrations: none. Thresholds, hard-conflict rules, policy, submission mode and the 1.0-year experience total
  unchanged.
- Verification: full suite 668 passed, 0 failed, 0 skipped. Live: backup `pre-m23-pipeline`; decisions snapshot
  before/after: no user status, note or job status changed; no duplicate requisitions.
  `jobpilot collect`: 16 sources ok, 155 fetched, 20 new, 4 updated, 131 unchanged, 0 duplicates, 0 expired.
  `jobpilot rematch`: 165 jobs (62 gained 6 location points; 8 family changes). Preview: 2 pass every condition
  (Rubrik #130, #131, over 70 because of the location fix), 2 blocked by Google sign-in, 161 excluded.
- Remaining / for you: profile gaps (acceptable locations lack Pune/Hyderabad/Chennai; no approved answer-bank
  entries; OOP / design patterns / system design / C++ appear in neither your skills nor your resume); old senior
  Workday jobs (#6–#10, #12) keep their flattened text until they reappear in a search.

## 2026-10-18 03:59 IST — Discovery for target employers, real-form validation, read-only preview

- robots.txt is now evaluated per RFC 9309 (longest rule wins) instead of `urllib.robotparser` (first match), which
  wrongly blocked Microsoft's permitted `/api/pcsx` and misread Google's file.
- New career-site adapters: `amazon_jobs`, `microsoft_careers`, `google_careers` (first page only, robots.txt
  disallows paging), `atlassian_careers`. Greenhouse/Lever/Ashby take optional title/location/exclude keywords.
  Greenhouse with keywords reads the light listing, then only the matches, so boards over 5 MB (Stripe, Databricks,
  Okta) work. Greenhouse `apply_url` is Greenhouse's own form when the posting links to the company's site.
- add-url refuses a Greenhouse board index or `?error=true` redirect (how the Appian non-job got in).
- Parser: "N+ years of <up to 60 chars> experience" ("non-internship professional software development") now
  parses, within one sentence. When a posting has a requirements section, skills elsewhere count as "mentioned"
  unless the wording is must-have (Google's "ideas from all areas, including system design…" boilerplate was
  marking system design/NLP as required).
- Forms (found on live Greenhouse forms): searchable drop-downs (`role=combobox`, react-select) are collected with
  their real labels and required flags, their hidden proxy inputs are skipped, and an approved answer is chosen only
  if one option equals it exactly (verified live). A chosen drop-down no longer counts as missing. An optional
  cover-letter upload no longer pauses a submission. Invisible score-based reCAPTCHA is named separately and still
  blocks. Assisted mode (`jobpilot assist`) now fills approved answers too.
- New read-only tools: `jobpilot autopilot preview|rehearse|status`, Telegram `/outcomes`.
- Files: `sources/{robots,careers}.py` (new), `sources/{http,ats,pages,__init__}.py`, `services/{job_parser,autopilot,
  telegram_bot,settings_store}.py`, `automation/apply.py`, `cli_autopilot.py` (new), `cli_ext.py`,
  `tests/test_m21_discovery.py` (new, 22), `tests/test_m22_forms.py` (new, 14), `docs/{USER_GUIDE,AUTOPILOT,TELEGRAM}.md`.
- Migrations: none (head still 0011).
- Verification: baseline 605 passed; final full suite 641 passed, 0 failed, 0 skipped. Adapters, parser fixes and
  collection were rehearsed on a copy of the database before going live. Real-form rehearsals: Rubrik and GitLab
  (Greenhouse), Mindtickle (Lever), all headless, nothing typed/uploaded/submitted except one non-personal drop-down
  choice on GitLab's form to verify selection; the page was closed unsubmitted.
- Live changes: backup `pre-m21-sources`; sources 5–17 added (Amazon, Microsoft, Google, Atlassian, GitLab, Zscaler,
  Rubrik, Stripe, Databricks, Okta, Coinbase, MongoDB, Mindtickle); first collection: 130 new jobs, 0 failures;
  `rematch` of 146 jobs; job #1 (Appian board page, not a vacancy) expired and dismissed; service restarted on the new
  code. Job #2 had already been refreshed via add-url (audit 21:33 UTC); re-running it was "unchanged".
  Autopilot unchanged: `submission_mode` manual, scheduler task disabled, batch approval required.
- Remaining / for you: decide how to handle Greenhouse's invisible reCAPTCHA (today: assisted submission only); fix
  the employment dates in your profile (TCS 2025-12-26 → 2025-12-29 makes "development experience" ~0 years); fill
  the answer bank (country, located in India, previous employment, links, primary language); Lever/Ashby submission
  and login-based platforms (Workday, Amazon, Microsoft, Google, iCIMS) remain unsupported.

## 2026-10-18 02:43 IST — Job descriptions: double-encoded HTML

- Job descriptions coming from page sources (JSON-LD / sitemap) now go through the new
  `text_utils.description_to_text`. HTML is converted once. A second decode happens only when the value contains
  encoded tags (`&lt;p&gt;…`) and the first pass still leaves real closing tags. Plain text (no closing tag or
  `<br>`) skips the HTML parser, so `List<String>`, `a < b` and a lone `<div>` mentioned in prose are kept.
  `description_raw` still stores the description exactly as received.
- Files: `src/jobpilot/services/text_utils.py`, `src/jobpilot/sources/pages.py`, `tests/test_m20_descriptions.py` (new).
- Migrations: none. Matching thresholds, eligibility policy, profile and autopilot settings unchanged.
- Verification: full suite 605 passed, 0 failed, 0 skipped. Synthetic postings only; job #2's stored raw
  description was checked by hand (read-only) and comes out as clean text (3144 chars, no tags).
- Remaining: stored descriptions change only when a posting is ingested again. Job #2 came in as a manual entry, so
  normal collection never refreshes it; re-add it from its URL to fix it. Workday/sitemap jobs were already clean.

## 2026-10-18 02:23 IST — Experience parser: months and reversed wording

- `parse_experience` now reads requirements stated in months ("0 to 12 months of experience" → 0–1 year; months are
  converted to years, the stored unit) and in reversed order ("experience of 8–12 years", "Experience: 3+ years"),
  with hyphen, en dash and em dash ranges. An explicit zero stays distinct from "not stated".
- Files: `src/jobpilot/services/job_parser.py`, `tests/test_m19_experience.py` (new).
- Migrations: none. Policy, thresholds, hard-conflict rules and your stated experience unchanged.
- Verification: full suite 589 passed, 0 failed, 0 skipped. Live: backup `pre-rematch-parser`, then `jobpilot rematch`
  (16 jobs). Changed: #2 experience check unknown→pass (0–1 yr; eligibility stays unknown: no required skills
  recognised); #10 unknown→ineligible with a hard conflict (8–12 yrs). Other 14 jobs, sightings, applications,
  events and user decisions unchanged.
- Remaining: stored `jobs.experience_min_years` columns refresh on the next collection of each posting (matching
  re-parses descriptions, so scores and eligibility are already current).

## 2026-10-18 01:54 IST — Autonomous applications (autopilot), Greenhouse submission adapter, Telegram batch control

- Standing-policy pipeline: prepare suitable jobs automatically, authorize by Telegram batch or standing policy,
  submit within a daily limit, verify the outcome (confirmed / failed / uncertain), exceptions to the review queue
  and Telegram. Off by default (`autopilot.submission_mode: "manual"`); live setting not changed.
- Fixed: CAPTCHA detection only looked at loaded frame URLs; now also the page markup (iframe/script src, widgets).
- Files: `services/autopilot.py` (new), `models/autopilot.py` (new), `automation/apply.py`,
  `services/{applications,notifications,queue,scheduler,settings_store,telegram_bot}.py`, `integrations/telegram.py`,
  `models/{__init__,notifications}.py`, `web/routes/core.py`, `web/templates/system.html`,
  `migrations/versions/0011_autopilot_batches.py` (new), `tests/test_m18_autopilot.py` (new), `docs/AUTOPILOT.md` (new).
- Migrations: 0011 (`submission_batches`, `notifications.reply_markup`).
- Verification: full suite 566 passed, 0 failed, 0 skipped. Form submission tested only against synthetic
  Greenhouse-style pages on 127.0.0.1 with headless Chrome; no live employer, email or Telegram message was used.
- Manual / remaining: activation steps in `docs/AUTOPILOT.md`; Lever submission, Workday and other ATS unsupported.

## 2026-10-18 01:18 IST — Implementations log rule

- Added the rule that every implementation is recorded here with a timestamp (`CLAUDE.md`), and created this log.
- Files: `CLAUDE.md`, `implementations.md`.
- Migrations: none.
- Verification: documentation only; no code changed, no tests needed.
