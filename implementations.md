# Implementations log

Every implementation in this repository, newest first, with a local timestamp. See `CLAUDE.md` for the rule and the
entry format. Earlier milestones (before this log existed) are summarised in `IMPLEMENTATION_STATUS.md`.

## 2026-11-01 20:50 IST — Master resume v3 (new backend skills)

- Why: you asked for the Master resume to list the backend skills added to the hub, so tailored resumes include them.
- `ArthamBhardwaj_Master_v3.tex` = v2 with only TECHNICAL SKILLS changed: Shell Scripting (Languages); Spring
  Security, Spring Cloud (Backend; JPA/Hibernate moved before Actuator/Micrometer so the line doesn't overflow);
  a new "System Design & Security" line; Linux, Nginx and "AWS (EC2, S3, Lambda, RDS, SQS)" (DevOps). Compiled
  locally (2 pages, no new overfull boxes), imported as Master resume v3, reviewed (diff = the skills lines only;
  13 skills added, none removed, no unconfirmed skills, no contradictions), approved with acknowledgement, and
  its LaTeX registered as the template (`templates/master_v3_6cddd5890b7c.tex`). 43 earlier tailored resumes are
  now marked outdated; v1 and v2 stay approved.
- Files: `howtouse.md` (Master resume section). Data: the new source, PDF and template under `data/files` (not in
  git).
- Migrations: none.
- Verification: `jobpilot resume generate --job 203` made a new tailored resume from v3: valid, 13/13 checks passed,
  and the new skills appear in it.
- Left to do: none. Tailored resumes for other jobs are rebuilt when you next ask for them.

## 2026-11-01 20:29 IST — Backend skills added to the information hub

- Why: you said you've learned these backend skills and want them used for resume generation and matching.
- Skill dictionary: new built-in skills caching, load balancing, database sharding, rate limiting, oauth2, jwt,
  api security, nginx; AWS now also recognises "AWS Lambda", "Amazon/AWS RDS", "Amazon/AWS SQS".
- Profile data (not code): 13 confirmed skills added as self-reported (Spring Security, Spring Cloud, System Design,
  Caching, Load Balancing, Database Sharding, Rate Limiting, OAuth2, JWT, API Security, Linux, Shell Scripting,
  Nginx); notes added to AWS (EC2, S3, Lambda, RDS, SQS) and Hibernate (Spring Data JPA). 48 skills now.
- Files: `src/jobpilot/services/skills_taxonomy.py`.
- Migrations: none.
- Verification: full `pytest` suite passed (exit 0). `jobpilot rematch` re-scored 562 jobs. Docker rebuilt and healthy.
- Left to do: tailored resumes only reorder what your Master resume's LaTeX already lists, so these skills don't
  appear on the PDFs until the Master resume's TECHNICAL SKILLS section includes them and the template is
  re-registered.

## 2026-11-01 19:13 IST — Tailored resume PDFs (and LaTeX) from Telegram

- Why: you asked that a resume requested on Telegram is made from LaTeX and sent to you as a PDF.
- New connector tools: `create_tailored_resume(job_id, regenerate)` (the existing tailoring: your Master resume's
  LaTeX, matching content first, nothing invented, compiled locally with pdflatex / latexmk, validated, reused if
  nothing changed); `send_resume_to_telegram(resume_id, include_latex)` (only valid resumes whose PDF matches its
  checksum, only to your configured chat; recorded in the activity log); `add_job_from_text` (a pasted job
  description). The assistant's instructions say to use them for resume requests.
- `TelegramClient.send_document` (multipart sendDocument, same retries / errors as text);
  `tailored.latex_source` rebuilds a resume's LaTeX and refuses unless it matches the compiled source's checksum
  (only the PDF is stored).
- Files: `mcp_server.py`, `services/assistant.py`, `services/tailored.py`, `integrations/telegram.py`,
  `tests/test_m46_assistant.py`, `howtouse.md`.
- Migrations: none.
- Verification: `pytest tests/test_m46_assistant.py tests/test_m34_tailored.py`: 38 passed (real pdflatex; Telegram
  faked); then `tests/test_m46_assistant.py` again after the activity-log line: 13 passed. Full `pytest` suite passed
  (exit 0). Live: helper restarted and Docker rebuilt; a request queued as the bot would ("make a tailored resume for
  job #203 and send me the PDF") was handled in 77 s / 4 steps: it reused the valid resume R45 (Mastercard, 2 pages)
  and sent it, no errors in the helper's journal. Receipt on your phone not checked by me.

## 2026-11-01 18:45 IST — /usage in Telegram: Claude plan limits and assistant use

- Why: you asked to check Claude usage from the Telegram bot.
- The helper now reads Claude Code's stream-json output: after every answer it saves your plan's 5-hour and weekly
  limits (percent used, reset time) to `data/assistant/limits.json`, and appends tokens, time, turns and the
  API-equivalent cost (not charged on your plan) to `data/assistant/usage.jsonl`.
- `/usage`: with the helper running, it refreshes the limits with a one-word Haiku call (no tools, nothing saved as a
  conversation) and replies with the report; otherwise the bot shows the last known numbers, marked possibly out of
  date. Report: 5-hour and weekly limit, questions today and in 7 days, tokens, average time, model. `/usage` and
  `/new` added to `/help` and the bot's command menu (setMyCommands sent; no messages).
- Files: `services/assistant.py`, `services/telegram_bot.py`, `cli_telegram.py`, `tests/test_m46_assistant.py`.
- Migrations: none.
- Verification: `pytest tests/test_m46_assistant.py tests/test_m25_telegram_replies.py tests/test_m27_automation.py
  tests/test_m14*.py`: 50 passed. Full `pytest` suite passed (exit 0). Live: helper restarted, Docker rebuilt;
  `/usage` handled by the bot in Docker was answered by the helper with fresh numbers (5-hour 12%, weekly 29%) and
  sent to your Telegram.
- Note: the limits are for your whole Claude plan (this Claude Code session included), not just the bot.

## 2026-11-01 18:25 IST — Ask Claude: a JobPilot connector for Claude Code, and Claude over Telegram

- Why: you asked to just type and have Claude do the work, without extra cost. Uses your Claude Code sign-in
  (no API key; counts toward your plan's usage).
- Connector (`jobpilot mcp`, MCP over stdio, `mcp` 2.x library, new optional extra `assistant`): 14 tools returning
  short text — overview, search_jobs, job_details, set_job_status, add_job_from_url (LinkedIn refused),
  list_applications, find_contacts, list_drafts, read_draft, draft_outreach_email, edit_draft, read_hiring_post,
  save_hiring_post_lead, followups_due. None can approve or send email, submit applications, delete or change
  settings. It never migrates the database (the running app owns the schema).
- Telegram: a plain (non-command) message is dropped into `data/assistant/inbox/` when the helper is running (its
  heartbeat file is under 90 s old) and the bot answers "🤖 Asking Claude…"; otherwise the old "I only understand
  commands" reply. `/new` starts a new conversation. The helper (`jobpilot assistant run`, on the host) runs
  `claude -p` with only the JobPilot connector (`--tools ""`, `--strict-mcp-config`, `--allowedTools mcp__jobpilot`,
  in an empty folder, message on stdin), model Sonnet by default (`JOBPILOT_ASSISTANT_MODEL`), continues the same
  conversation for 2 hours, and sends the reply as plain text from the host (only the bot in Docker polls Telegram).
- `jobpilot assistant setup` registers the connector with Claude Code (user scope) and writes
  `~/.config/systemd/user/jobpilot-assistant.service`; `jobpilot assistant status`.
- Files: `mcp_server.py` (new), `services/assistant.py` (new), `cli_assistant.py` (new), `cli_ext.py`,
  `services/telegram_bot.py`, `pyproject.toml`, `howtouse.md`, `tests/test_m46_assistant.py` (new).
- Migrations: none.
- Verification: `pytest tests/test_m46_assistant.py`: 9 passed (Claude faked). Full `pytest` suite passed (exit 0).
  Live: tools read the real database correctly; `claude -p` through the connector answered from real data in 7 s,
  continued a conversation, and confirmed it has no shell or file tools; `claude mcp get jobpilot` shows Connected;
  Docker rebuilt (`./jobpilot-docker update`), its bot sees the helper; a test message queued in Docker was answered
  and sent to your Telegram chat with no error logged (delivery on the phone not checked by me).
- Manual action: starting the helper at login was blocked by the permission check. Run
  `systemctl --user daemon-reload && systemctl --user enable --now jobpilot-assistant`. Until then it only runs while
  this Claude Code session keeps it running.
- Update 18:32 IST: at your request, ran `systemctl --user enable --now jobpilot-assistant` (enabled, active). A test
  message queued in Docker was answered by the service and sent to Telegram (no errors in its journal).

## 2026-11-01 17:46 IST — Docker copy updated; expired Gmail sign-in is a health warning, not a failure

- Why: after `./jobpilot-docker update` the container was marked unhealthy, because the 16:08 change made an expired
  Gmail sign-in fail the health check (`/health` returned "not ok", which Docker's HEALTHCHECK reads). The app was
  running fine, and a restart can't fix a sign-in.
- The Gmail check now stays `ok` with `warn: true` and the "sign-in expired" detail; the System page shows ⚠ for it.
- Files: `services/health.py`, `web/templates/system.html`, `tests/test_m13_gmail.py`.
- Migrations: none.
- Verification: `pytest tests/test_m13_gmail.py tests/test_stage6.py`: 41 passed. Ran `./jobpilot-docker update`
  (it takes a pre-update backup first): container "healthy", `/health` ok, `/drafts/from-post` returns 200 in Docker.
- Manual action: renew the Gmail sign-in (16:08 entry).

## 2026-11-01 17:39 IST — Draft an email from a pasted LinkedIn hiring post; Profile page sections restored

- Why: you asked to reach people who post "we're hiring" on LinkedIn. Instead of scraping the feed (against LinkedIn's
  terms, risks the account), you paste a post you saw and JobPilot reads it.
- New page **Outreach → From a LinkedIn post** (`/drafts/from-post`): paste the post text (and optionally its link), and
  JobPilot reads the role, company, poster's name and headline, recruiter or hiring manager, location and the email
  address, including "name [at] company [dot] com" forms. It warns when the post has no address or says "DM only" /
  "apply via the link". You check every field and tick that the poster published the address. Saving creates a job lead
  (the post is its description), a contact (source "LinkedIn (found by you)", email *unverified*) and a draft from the
  recruiter or hiring-manager template. Nothing is sent: you approve and send from Email drafts as usual. The
  do-not-contact list, one-draft-per-recruiter-per-job and daily send limits apply, and automatic outreach skips it
  because the address is unverified. The link must be linkedin.com and is stored without tracking parameters; JobPilot
  never fetches it.
- Bug fixed: the menu redesign (01:30 entry) named its menu list `sections`, the same name the Profile page uses, so
  Profile showed none of its Skills / Employment / Education … sections. Renamed to `nav_sections` in `base.html`. This
  caused the earlier profile / acceptance / browser-form test failures.
- Files: `services/hiring_posts.py` (new), `web/templates/hiring_post.html` (new), `web/routes/outreach.py`,
  `web/templates/base.html`, `web/templates/drafts.html`, `tests/test_m45_hiring_posts.py` (new),
  `tests/test_stage6.py` (reads the new menu format, so it opens every menu page again), `howtouse.md`.
- Migrations: none.
- Verification: `pytest tests/test_m45_hiring_posts.py`: 12 passed. Full `pytest` suite passed with nothing
  excluded (exit 0), including the 4 failures noted in the 16:08 entry. Live on a throwaway instance (empty database,
  port 8798, Gmail off, stopped after): pasted a synthetic post, the fields were read correctly, saving gave a draft
  to the right address; screenshot of the review step checked; Profile shows its sections again. Gmail sending of such
  a draft was not tried (your Gmail sign-in is still expired).
- Manual action: `./jobpilot-docker update` to get this in the Docker copy. Gmail sign-in still needs renewing (16:08 entry).

## 2026-11-01 16:08 IST — Gmail: report an expired sign-in instead of claiming "authorized"

- Why: email stopped working. Google rejects the stored refresh token (`invalid_grant`, confirmed live with
  `jobpilot gmail check`), so every send and reply check failed, while `gmail status`, Settings and System health still
  said "authorized". Cause: the Google Cloud OAuth app is in Testing mode, whose sign-ins expire after 7 days.
- On `invalid_grant` the token file is now marked with the error; `gmail status` (`authorized: false`, `auth_error`),
  readiness (shown before sending) and the health check (`ok: false`, "sign-in expired") report it. `gmail auth`
  writes a fresh token, which clears it. The error message now names the Testing-mode cause.
- `docs/GMAIL_SETUP.md`: publish the OAuth app (In production) instead of leaving it in Testing.
- Files: `integrations/gmail.py`, `services/health.py`, `tests/test_m13_gmail.py`, `docs/GMAIL_SETUP.md`.
- Migrations: none.
- Verification: `pytest tests/test_m13_gmail.py` passed. Full suite: 4 failures, none in email code (UI and profile work
  already in progress): `test_acceptance::test_02_to_13_full_workflow` (re-run with this change stashed, same result),
  `test_m11_profile::test_profile_page_shows_and_saves_new_sections`, `test_stage2_web::test_profile_flow`,
  `test_stage6::test_every_page_renders_on_empty_database` (reads the old sidebar format from base.html). Live: after the
  change, `jobpilot gmail check` hit invalid_grant and `gmail status` then showed `authorized: false`.
- Manual action: publish the OAuth app in Google Cloud Console (Audience → Publish app), then run
  `.venv/bin/jobpilot gmail auth`. Rebuild Docker (`./jobpilot-docker update`) for the new status reporting there.
- Not done: LinkedIn feed scraping (declined: breaks LinkedIn's terms and risks the account).

## 2026-11-01 01:40 IST — Fintech jobs page

- Why: you asked for a separate section for fintech jobs (payments, broking, banks, exchanges, trading, crypto).
- `services/sectors.py` (new): ~110 fintech, bank, exchange, trading-firm and crypto company names, matched as whole
  words in the company name ("JPMorgan Chase & Co." yes, "Citizens Advice" no), in Python and as a SQL filter.
- `jobs.search(sector="fintech")`; new page **Find jobs → Fintech jobs** (`/fintech`): the Jobs page limited to those
  companies, with the same level / role / region tabs, search, paging and PDF / Excel export
  (`/jobs/export.*?sector=fintech`). The Jobs page itself is unchanged.
- Files: `services/sectors.py` (new), `services/jobs.py`, `web/routes/jobs.py`, `web/templates/jobs.html`,
  `web/templates/base.html`, `tests/test_m44_fintech.py` (new), `howtouse.md`.
- Migrations: none.
- Verification: `pytest tests/test_m44_fintech.py tests/test_m13_ui.py tests/test_m35_regions.py` passed; full `pytest -x` suite passed (exit 0).
- Manual action: rebuild the Docker copy to see it there (`./jobpilot-docker update`). To add a company, add its name
  to `FINTECH` in `services/sectors.py`.

## 2026-11-01 01:30 IST — New look: "Midnight" theme and a menu in six sections

- Why: you asked for a complete UI change, tidier tabs and a cooler theme. The 21-item sidebar was one long list.
- Menu: six collapsible sections with icons. **Home** (Overview, Analytics); **Find jobs** (Jobs, Matching, Sources);
  **Apply** (Job desk, My tracker, Applications, Interviews, Review queue, Agents); **Outreach** (Recruiters,
  Companies, Email drafts); **You** (Profile, Information hub, Resumes, Answer bank, Skill gaps); **Settings**
  (Settings, System). The current section is open, the topbar shows "Section / Page", and every URL is unchanged.
- Theme: dark "Midnight" by default (navy surfaces, cyan → violet accent, glass topbar and sidebar, soft glows instead
  of offset shadows); light "Daylight" via the topbar toggle, remembered in the browser (`static/theme.js` runs before
  first paint, because the CSP blocks inline scripts). Component class names are unchanged, so every page picks it up.
- Files: `web/templates/base.html`, `web/static/app.css`, `web/static/app.js`, `web/static/theme.js` (new),
  `tests/test_m13_ui.py` (dark default with a light option; new test for the sections, breadcrumb and toggle),
  `howtouse.md`.
- Migrations: none.
- Verification: `pytest tests/test_m13_ui.py tests/test_m40_desk.py tests/test_m12_hub.py` passed; full `pytest -x` suite
  passed (exit 0). Screenshots checked in system Chrome (Overview, Jobs, Agents, Profile in dark; Overview, Jobs in light; Jobs
  and the open menu at phone width) on a throwaway instance with an empty database (port 8799, stopped after).
- Manual action: the Docker copy still serves the old UI until it is rebuilt (`./jobpilot-docker update`).

## 2026-11-01 00:58 IST — Fintech and bank / exchange tech-centre job sources

- Why: target Java backend SDE-1 roles where capital-markets (exchange) experience counts.
- New presets (Sources → Add regional sources): Greenhouse — Stripe, Coinbase; Workday — Mastercard, Morgan Stanley,
  Deutsche Bank, Nasdaq, State Street, Northern Trust, LSEG, Broadridge, Barclays. Same title / seniority / region
  filters as the other presets. Left out: BlackRock and FIS (no India engineering roles); PhonePe, Razorpay, Upstox,
  Angel One, Dhan, smallcase, Cashfree, Pine Labs (no Greenhouse or Lever board; add their career pages by hand).
- Files: `services/regional_sources.py`, `tests/test_m43_roles_sources.py`.
- Migrations: none.
- Verification: each board probed live on 2026-11-01 (India engineering postings found on every added board);
  `pytest tests/test_m43_roles_sources.py tests/test_m35_regions.py tests/test_m8_sources.py` passed (102 tests); full `pytest` suite passed (exit 0).
- Manual action: click Sources → "Add regional sources" (or `jobpilot sources regional`) to add them.

## 2026-10-31 15:25 IST — Only SDE / backend roles; 24 more job sources incl. Y Combinator

- Why: you asked to keep only relevant SDE + backend roles and to add more platforms / sources.
- Role filter (`services/roles.py`, title only): keeps software engineer / developer / SDE, backend, full stack and
  backend-stack developer titles; leaves out QA / test / SDET, support, sales, solutions engineers, mobile (Android,
  iOS, React Native, Flutter), frontend-only, DevOps / SRE, data / ML-only, firmware / embedded / kernel, low-code
  (Salesforce, AEM, n8n …) and gig listings (AI coding tasks, open-source contributor, talent pools). The cleanup now
  hides such jobs with reason "role" ("not an SDE / backend role"), reversibly; new setting
  `job_cleanup.sde_backend_only` (default on). Source presets' exclude list gained the same families.
- New source kind `yc_jobs` (`sources/yc.py`): Y Combinator's job board (India + remote software roles), listing data
  embedded in the page + JSON-LD per posting; country codes written out for the region filter; postings older than
  180 days skipped. robots.txt allows it.
- New presets (Sources → Add regional sources, also added by the Search agent): Greenhouse — Pure Storage, InMobi,
  HackerRank, Groww, Toast, Druva, Twilio; Lever — Meesho, Paytm, CRED, Zeta, FamPay; Workday — Citi, Salesforce,
  NVIDIA, Intel, Wells Fargo, Target, Motorola Solutions, Workday, Thomson Reuters, PayPal, CrowdStrike; YC.
  Checked and left out: SmartRecruiters, Recruitee (robots.txt disallows their APIs), Workable (public endpoints
  return no jobs), The Muse (403 to automated requests), Arbeitnow (Germany only), Naukri / LinkedIn / Indeed /
  foundit / Wellfound (terms forbid automated collection).
- Files: `services/roles.py`, `sources/yc.py` (new); `services/cleanup.py`, `services/settings_store.py`,
  `services/regional_sources.py`, `services/board_discovery.py`, `sources/__init__.py`, `howtouse.md`,
  `docs/ARCHITECTURE.md`, `docs/AGENTS.md`; tests `tests/test_m43_roles_sources.py` (new, 49 tests).
- Migrations: none.
- Verification: new tests 49 passed; full suite **921 passed, 0 failed**. Live (real network, nothing stored): every
  new source fetched without errors; SDE/backend roles not above SDE-1 by title — Citi 18, Motorola 11, Intel 6,
  NVIDIA 3, YC 1, Workday 1, Paytm 1; the other boards have none at that level today. On a copy of your database:
  the cleanup would hide 37 jobs, all for role (e.g. Android, React JS, SRE, AI coding tasks, talent pools), and 24
  sources would be added. Your live database was not changed.
- Manual: `./jobpilot-docker update`, then Sources → Add regional sources and run them (or wait for the Search agent);
  the cleanup runs after the next collection (or Jobs → cleanup now).

## 2026-10-29 09:21 IST — Export jobs as PDF and Excel (Jobs page and Job desk)

- Why: to share job title, company and apply link with friends. **Export PDF** / **Export Excel** buttons on `/jobs`
  and `/desk` download every job matching the page's current filters, search and tab (all pages, up to 5,000):
  #, job title, company, location (with Remote/Hybrid), apply link (falls back to the source URL).
- Both files are written with the standard library only (no new dependencies, Docker lock unchanged): a minimal
  .xlsx (bold header, frozen first row, autofilter, clickable links) and an A4-landscape PDF (Helvetica, wrapped
  cells, repeated header per page, page numbers, clickable link annotations). Only http(s) links are exported.
- Routes: `GET /jobs/export.{pdf|xlsx}`, `GET /desk/export.{pdf|xlsx}` (other formats 404). Filter parsing for
  both pages moved into `_explorer_filters` / `_desk_filters` so the page and its export always agree.
- Files: `services/job_export.py` (new), `web/routes/jobs.py`, `web/routes/desk.py`, `web/templates/jobs.html`,
  `web/templates/desk.html`, `docs/USER_GUIDE.md`; tests `tests/test_m42_job_export.py` (new, 3 tests).
- Migrations: none.
- Verification: new tests 3 passed; related web tests (m35, m36, m38, m39, m40, m42, stage2_web, hardening_routes)
  89 passed; full suite 872 passed, 0 failed. Sample PDF rendered with pdftoppm and checked by eye; sample .xlsx
  opened with openpyxl (installed only in a scratch folder) with warnings as errors: header, links, freeze pane
  and filter read back correctly. Not opened in Microsoft Excel itself.
- Left: nothing required. Non-Latin text (e.g. CJK) shows as "?" in the PDF (built-in font); the Excel file keeps it.

## 2026-10-29 01:12 IST — Experience requirement fixed: the highest required figure, not the lowest

- Why: you reported job #317 (Photon, "Java developer | Offshore") shown as Fresher / SDE-1 with score 94 although it
  asks 7+ years. `parse_experience` kept the **smallest** figure in a posting, so "7+ years in software engineering …
  2+ years with CI/CD" was stored as 2 years: it passed the fresher filter and the gap counted as soft.
- New rule (`services/job_parser.py`): the requirement is the **highest required** figure. Figures in a preferred
  context are left out: preferred / nice-to-have sections (also inline headings such as "… Basic Qualifications: 7+ …
  Preferred Qualifications: 5+ …"), and per-sentence wishes (a plus, valued, ideally, desirable, an advantage …); used
  only when nothing else is stated. Also left out: advanced-degree-only alternatives (as before) and employer
  boilerplate ("With over 20 years of experience, our company …"). "X or Y" in one sentence counts as the lower.
  New patterns: "3+ years working with …", and bullets "- 3+ years of <long qualifier> experience". Sentences no
  longer split after "e.g." / "i.e.".
- Checked against all 380 jobs in your database before applying: 117 figures went up (e.g. #317 2→7; Amazon "SDE"
  postings whose Basic Qualifications ask 3+ years, previously 2 from a "2+ years of design" line; #381 2→5), none went
  down; spot-checked against the posting text. Real Amazon SDE-1 postings ("2+ years" basic, "3+ years" preferred)
  stay at 2.
- Safety net: the cleanup also hides any job with a hard conflict (new reason "not suitable (hard conflict)"), and the
  Re-score button and the scheduled rematch task run the cleanup afterwards.
- Migration 0018 (data only): re-parses years / seniority / fresher fit for every job and marks changed ones unscored,
  so the next collection or Re-score re-scores and re-checks them.
- Files: `services/job_parser.py`, `services/cleanup.py`, `services/scheduler.py`, `web/routes/jobs.py`,
  `migrations/versions/0018_reparse_experience.py` (new); tests `test_m41_experience_rule.py` (new, 15 tests),
  `test_m39_cleanup_search.py` (new reason in the counts).
- Verification: full suite 868 passed, 1 failed (only the new "conflict": 0 count in an expected dict), fixed, both
  affected files re-run green (22 passed). On a copy of your live database after 0018 + re-score + cleanup: #317 reads
  7+ years, "6 year(s) short", recommendation not suitable, hidden ("above SDE-1, not suitable"); 108 jobs newly
  hidden (13 also hard conflicts); 119 visible active jobs remain (51 fresher / SDE-1, 68 level not stated), **none**
  asking more than 2 years and none with a hard conflict.
- Manual: `./jobpilot-docker update`, then Jobs → Re-score all (or wait for the next collection).

## 2026-10-28 23:13 IST — Job desk tab: generate a resume for a job, apply, record and delete on one page

- Why: you asked for a separate jobs tab where you can delete or apply for any job, with "generate resume for this
  job" on the same page. (Per-job tailored resumes already existed, but only on each job's detail page.)
- New **Job desk** (`/desk`, sidebar under Apply, after Jobs). Tabs To apply / Saved / Applied / All with counts;
  same search syntax as Jobs; your focus by default ("Show all jobs" widens it). Each card: (1) **Generate resume
  for this job** → Download PDF / Regenerate / what changed, plus the recommended approved resume; (2) **Apply on
  company site ↗** / view posting; (3) **✓ I applied** with the resume used (tailored pre-selected when ready) and
  date, Save for later; and **Delete**. Every action returns to the same card (`#job-N`); the generate button shows
  "Generating…" while it works. A notice explains if generation is unavailable (switched off, no template, no LaTeX).
- **Delete** (`services/job_delete.py`): removes the job and what exists only for it (sightings, tailored resumes and
  their PDFs, match feedback, contact links, open review items); drafts and contact history keep their records with
  the job link cleared. Refused for jobs with an application (your record of applying). A new `deleted_jobs` table
  (fingerprint, canonical apply URL, source + external id) makes collection and job-alert import skip the posting
  (`skipped_deleted` in source stats); adding it yourself (manual / URL) clears that. Also "Delete job" in the job
  page's More actions.
- Generate route accepts a local `next` (Jobs / Job desk) and the tracker's "I applied" may return to `/desk`.
- Files: `services/job_delete.py`, `web/routes/desk.py`, `web/templates/desk.html`,
  `migrations/versions/0017_deleted_jobs.py` (new); `models/{jobs,__init__}.py`, `services/{jobs,job_alerts}.py`,
  `web/routes/{jobs,tracker,__init__}.py`, `web/templates/{base,job_detail}.html`, `web/static/{app.css,app.js}`;
  tests `test_m40_desk.py` (new, 4 tests).
- Migrations: 0017 (creates `deleted_jobs`). Applied only to a scratch copy of your database.
- Verification: full suite **854 passed**. `test_m40_desk.py` includes a real LaTeX generation from the desk, recording
  the application with the tailored resume, delete with PDF removal, and a deleted job being skipped when a source
  lists it again. On a copy of your live database: desk shows 216 to apply / 3 saved / 1 applied; a resume for the top
  job (Photon Interactive, Java developer) was generated from the desk with your real template, validated, and
  downloaded (81 KB PDF); desktop and phone screenshots checked.
- Manual: `./jobpilot-docker update` to get this into your running app (applies migration 0017 with a backup first).

## 2026-10-28 21:55 IST — Relevance cleanup of existing jobs (reversible) and a better Jobs search

- Why: you asked to keep only the relevant jobs and for better search on the Jobs page. (Timestamp approximate:
  shell commands were blocked at the end, so `date` could not be run.)
- **Cleanup** (`services/cleanup.py`): a job is relevant when it is fresher / SDE-1 or level not stated, in India /
  remote (open to you) / Dubai, in one of your role categories, and scores ≥ `job_cleanup.min_score` (40). Others are
  *hidden*, not deleted: `user_status = dismissed` (so Jobs page, agents, Telegram, review queue, skill gaps skip
  them, and a re-collected copy stays hidden; a deleted job would come back as new) plus new `jobs.hidden_reason`
  (`cleanup:level,region,…`). Never touched: jobs with an application, saved (shortlisted) or dismissed by you.
  Undo restores all; changing a job's status yourself clears its mark. Runs automatically after each collection
  (setting `job_cleanup.auto`, default on). UI: "Clean up N irrelevant" button (with confirm), "Hidden by cleanup (N)"
  view with the reason per job, Restore per job and Restore all. CLI: `jobpilot cleanup [-v] [--apply|--undo]`.
- **Search** (`services/search_query.py`): every word must match (whole words: java ≠ javascript), `"phrases"`,
  `-exclude`, `title:` `company:`/`at:` `location:`/`in:` `skill:`, skill synonyms from the skills dictionary
  (k8s ↔ kubernetes, postgres ↔ postgresql; ambiguous short aliases like "go"/"rest"/"node" only when typed).
  Ranked by where it matched (title > skills / company > location > description), then score. UI: large search box
  (`/` focuses it), removable query pills (negations red, "+N" synonyms), "Your skills" chips (your confirmed skills
  the jobs in view ask for most), highlighted matches, a description snippet when only the description matched,
  search tips, dropdowns apply on change, and "Show N matches at any level, role or region" when nothing matches.
- Files: `services/{cleanup,search_query}.py` (new), `services/{jobs,settings_store}.py`, `models/jobs.py`,
  `migrations/versions/0016_job_hidden_reason.py` (new), `web/routes/jobs.py`, `web/templates/jobs.html`,
  `web/static/{app.css,app.js}`, `cli_ext.py`; tests `test_m39_cleanup_search.py` (new, 8 tests),
  `test_m38_fresher_focus.py`, `test_stage2_web.py`, `test_m8_analytics.py` (cleanup off: its numbered synthetic
  titles read as SDE-2).
- Migrations: 0016 (adds indexed `jobs.hidden_reason`). Applied only to a scratch copy of your database.
- Verification: full suite 848 passed, 2 failed (expected changes: highlight markup; analytics fixture), both fixed and
  the six affected files re-run green; a final full-suite run was not repeated after the last two small changes
  (ambiguous-alias rule, hidden view lists expired jobs too). On a copy of your live database: preview found **155**
  to hide (150 above SDE-1, 5 not your roles, 6 low score; some have several reasons), applied there, 155 listed in
  the hidden view; searches rendered and screenshotted (kafka -senior: 10, rust golang: 4).
- **Not done (blocked by the permission check):** rebuilding / restarting your live Docker app and running the cleanup
  on your real data. Run: `./jobpilot-docker update`, then the Jobs page button or
  `./jobpilot-docker cli cleanup --apply`.

## 2026-10-28 21:31 IST — One-command Docker: `./jobpilot-docker`, LaTeX and pdftotext in the image

- Why: you asked to dockerize JobPilot so it is easy to run. The image and compose file already existed but needed
  ~8 manual steps, started on an empty volume, and lacked LaTeX (no tailored resumes in Docker).
- New `jobpilot-docker` script: `start` (makes `.env.docker` from the example + your `.env`, minus path / host / port
  keys, browser applying off, host time zone, mode 600; builds; waits for healthy), `stop`, `restart`, `status`,
  `logs`, `update` (backup, rebuild, restart), `cli <cmd>`, `switch-to-docker` (asks, disables the native systemd
  service, backs up, starts) and `switch-to-native`. Mounts this repository's `data/` and runs as your uid, so no
  export / import. Refuses to start on your data while the native service is running or enabled, and refuses an
  occupied port; a separate copy (other data folder and env file) may run alongside.
- Dockerfile: runtime adds `poppler-utils` and (build arg `WITH_LATEX`, default true) `latexmk`,
  `texlive-latex-base`, `texlive-latex-recommended`, `texlive-fonts-recommended`; `TEXMFVAR` / `TEXMFCONFIG` in /tmp
  for the read-only root. Image 197 MB → 487 MB.
- Files: `jobpilot-docker` (new), `Dockerfile`, `README.md` (Docker quick start), `docs/DOCKER.md` (new "easy way"
  section; included / not-included tools updated).
- Migrations: none.
- Verification (live, Docker): image built; an isolated test project (`jobpilot-test`, port 18765, a copy of your
  database and resume templates, Telegram / Gmail / scheduler off) started via the script and became healthy;
  migrations ran on the copy to 0015; `/jobs` showed 218 jobs; your real Master template compiled to a PDF inside
  the container (latexmk); `pdftotext` present; files on the host owned by uid 1000; `stop` logged
  `shutdown complete`; test project removed. `start` on your real data was refused while the native service runs.
  The `.env` → `.env.docker` merge was tested with fake values only (your real `.env.docker` was not created).
  Not run: `switch-to-docker` / `switch-to-native` (they change your systemd service), `update`.
- Not included: Chrome / Playwright, so browser-assisted applying and the Apply agent's form submission still need
  the native install. Native service currently reports 503 (database 0014, head 0015) until it is restarted.

## 2026-10-28 21:15 IST — Jobs page focused on fresher / SDE-1 roles in India, Dubai and remote; card layout

- Why: you asked that only SDE-1 / fresher roles for your target roles in India, Dubai and remote show, and for a
  better-looking Jobs page.
- New `services/levels.py`: each job gets `level_fit` = `fit` (entry title such as SDE I / junior / new grad /
  graduate / fresher, or at most 2 years asked), `no` (senior / lead / staff / SDE II / Engineer 2 / III /
  intermediate / mid-level / internship, or more than 2 years asked) or `unstated`. Set at ingest; backfilled by the
  migration. Title and stated years only.
- Jobs page defaults: Fresher / SDE-1 (fit + not stated), only India / Remote (open to you) / Dubai, only your role
  categories. Toggles: Experience (Fresher / SDE-1 · Confirmed only · All levels) and Roles (My target roles · All
  roles); "Hiding N above SDE-1 and M outside your roles" note. The Elsewhere tab is gone (`region=any` / `region=other`
  still work by URL). Region tab counts now apply every other filter.
- Redesigned list: job cards (score, title, company · location, region / level / years / role / eligibility badges,
  posted date, Apply / Resume & record / Save / Dismiss) instead of the wide table; stacks on phones.
- Dashboard "Jobs by region" → "Fresher & SDE-1 jobs by region", same focus.
- Files: `services/levels.py` (new), `services/jobs.py`, `services/regions.py`, `models/jobs.py`,
  `migrations/versions/0015_job_level_fit.py` (new), `web/routes/jobs.py`, `web/templates/{jobs,dashboard}.html`,
  `web/static/app.css`, tests `test_m38_fresher_focus.py` (new, 19 tests), `test_m35_regions.py`,
  `test_m36_apply_ui.py`, `test_stage2_web.py` (updated for the new defaults and markup).
- Migrations: 0015 (adds indexed `jobs.level_fit`, backfills it). Applied to a scratch copy of your database only; it
  applies to the real database on the next service start (after 0014).
- Verification: full suite **843 passed**. On the scratch copy: 218 jobs shown by default (149 confirmed fresher /
  SDE-1, 69 level not stated), 142 above SDE-1 and 4 outside your roles hidden; India 147, Remote 98, Dubai 0 (the
  5 Dubai jobs all ask 3+ years). Pages rendered in-process and screenshotted with headless Chrome at desktop and
  phone width.
- Not changed: the Agents' job picking and Telegram digests still use their own rules (score / eligibility), not this
  level filter.

## 2026-10-25 00:02 IST — Four agents: Search, Resume, Apply and Manager (automatic applying where safe)

- Why: you asked for the job search and applying to run as agents with as little of your time as possible. You chose
  rules only (no AI model), auto-submit when safe, all extra search channels, and checked rewording.
- **Manager** (`agents/manager.py`, scheduler task `agents`, default every 3 h, **off until you turn it on**): picks
  active jobs by policy (Settings → `agents`: score ≥ 70, eligible / likely eligible, not applied, not waiting on you,
  company cooldowns, max 5 per cycle, 10 per day), holds dream companies for you, runs the three agents, saves each
  agent's report and sends a Telegram summary (`agents_digest`). Starts in **dry_run** (nothing sent or submitted).
- **Search** (`agents/search.py`): adds missing feed / regional presets, runs all sources (every 6 h), reads job-alert
  emails (`services/job_alerts.py`; LinkedIn, Naukri, Indeed, Glassdoor, foundit, Instahyre; needs
  `JOBPILOT_GMAIL_ALLOW_READ_BODIES=true`), and adds the Greenhouse / Lever boards of companies seen only in feeds or
  alerts (`services/board_discovery.py`; board name must be the company; Ashby skipped, its robots.txt disallows the
  API). New feed adapters `sources/feeds.py`: Remote OK (credited), Working Nomads, We Work Remotely RSS (robots.txt
  checked 2026-10-24).
- **Resume** (`agents/resume.py`): tailored resume per picked job, falls back to the approved resume. New rule-based
  rewording (`services/wording.py`, setting `tailored_resumes.reword_skills`, on): a skill you list is spelled the way
  the posting spells it, from a fixed table of pure synonyms (Postgres/PostgreSQL, K8s/Kubernetes, REST/RESTful APIs,
  …); each swap goes to the validator as an allowed correction.
- **Apply** (`agents/apply.py`): (1) email application when the posting asks for it (address must be in a sentence
  asking for applications; privacy / legal / no-reply excluded), sent via the existing Gmail send path with the
  tailored PDF attached (new `email_drafts.tailored_resume_id`, migration 0014); (2) Greenhouse / Lever / Ashby forms
  via new `automation/apply.py::submit_page` / `run_submission`: submits only with no CAPTCHA / bot check / login, all
  required fields approved (required consent boxes always stop it), exactly one submit control, and a re-check right
  before the click; attempt recorded before the click; unclear results never retried (logic from the archived
  autopilot); (3) everything else → review item `agent_needs_you` with the one step (often `jobpilot assist …`).
  Recording "I applied" closes these items.
- UI: new **Agents** page (`/agents`, sidebar): mode, pause, schedule, Needs you (copyable assist command), each
  agent's last report, policy form. CLI: `jobpilot agents run|status|mode|pause|resume`. Docs: new `docs/AGENTS.md`;
  `howtouse.md` §0 and rules, README, USER_GUIDE, ARCHITECTURE updated (they said JobPilot never applies).
- Files: `src/jobpilot/agents/{__init__,base,search,resume,apply,manager}.py`, `cli_agents.py`, `cli_ext.py`,
  `sources/{feeds,__init__}.py`, `services/{wording,board_discovery,job_alerts,tailored,regional_sources,settings_store,
  scheduler,drafts,tracker}.py`, `integrations/gmail.py`, `automation/apply.py`, `models/outreach.py`,
  `migrations/versions/0014_agents.py`, `web/routes/{agents,__init__}.py`, `web/templates/{agents,base}.html`,
  `web/static/app.css`, docs above, `tests/test_m37_agents.py` (14 tests).
- Migrations: 0014 (adds `email_drafts.tailored_resume_id`). Applied to a scratch copy only; it applies to the real
  database on the next service start.
- Verification: full suite **824 passed** (final run, after the Agents-page fix); `tests/test_m37_agents.py` 14 passed (incl. real
  headless-Chrome submissions to a fake employer form on 127.0.0.1: confirmed, CAPTCHA, missing answer; and a real
  LaTeX compile of a reworded resume passing all checks). Live, read-only: the three feeds fetched (Remote OK 5
  matches, We Work Remotely 27, Working Nomads 0), board probes found GitLab / Rubrik (Greenhouse) and Mindtickle
  (Lever), a dry-run cycle on a copy of your database picked 6 jobs, made 6 tailored resumes (all checks passed) and
  handed all 6 to you (3 Google held, Mastercard Workday login, Canonical via Jobicy, Rubrik invisible reCAPTCHA);
  a read-only rehearsal of the live Rubrik form confirmed the reCAPTCHA plus privacy / NDA questions. Mocked only:
  Gmail (fake), alert-email parsing (synthetic emails; real alert layouts not seen), board APIs in tests.
  **Not done: no real application was sent or submitted.**
- Manual: restart the service; Agents → Turn on the schedule; check a dry-run cycle; then Switch to automatic. For
  alert emails: `JOBPILOT_GMAIL_ALLOW_READ_BODIES=true` in `.env`, `jobpilot gmail auth`, restart.

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
