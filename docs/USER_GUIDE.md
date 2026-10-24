# JobPilot user guide

## 1. Profile and verified facts

**Profile** holds core details (name, contact, location, experience, notice period, salary
expectations, work authorisation, relocation, preferred job titles, job-search priorities) and fact lists:
skills, employment, education, projects, certifications, achievements, links. Every field is optional.

Each fact has a status:

| Status | Meaning | Used for scoring / resumes / drafts / autofill? |
|---|---|---|
| confirmed | You verified it | Yes |
| learning (skills only) | You're working on it | Shown as "learning"; never claimed |
| unverified | Recorded but not confirmed (e.g. detected in a resume) | No |
| outdated / rejected | No longer accurate, or wrong; kept for history | No |

New facts are always **unverified**; nothing is promoted to confirmed except by you.

**Kind of work** (employment): software development, testing / QA, data, DevOps, support, other. Only roles
you mark *software development* count as development experience. A development job's years requirement
is a **warning** (not a pass) when your years come from testing or other work, and a role without a recorded
kind of work is flagged rather than guessed.

**Project kind**: personal, academic, learning exercise, open source. No project ever counts as work
experience. Interview prompts and preparation records label them as such.

**Achievements** record where they happened (work / academic / project / other). Enter only what you can
back up; no estimated metrics.

Editing core details clears their confirmation; click *Confirm details* again. Experience years come
from your stated total, or are computed from confirmed full-time roles. If neither exists,
experience is **unknown** and eligibility checks say so.

## 1a. Information hub (import your resume once)

**Information hub** (`/hub`) shows your whole profile in one place: details, links, employment, education, skills
with their evidence, projects, certifications, achievements, preferences, learning, answers, resume versions and
imports. It is a view over the profile, not a second copy: editing and confirming still happen on the same records.

**Importing a resume** fills the profile from the file instead of typing:

- Web: *Information hub → Resumes and imports* → *Import into profile* on a resume version, or upload a new file
  (tick *dry run* to see the report without saving).
- CLI: `jobpilot hub import --file ~/path/resume.pdf` (or `--version ID`, add `--dry-run` to preview);
  `jobpilot hub report` shows the latest report.

What the import does:

1. Stores the file unchanged (SHA-256 recorded). The exact same file is never registered twice; a new file becomes
   a **draft** version of your master resume. Approving it stays your decision.
2. Extracts text locally (`pdftotext` from poppler-utils if installed, otherwise pypdf; PDF link targets such as
   LinkedIn/GitHub are read from the file). Nothing is sent anywhere; no AI is used.
3. Parses the sections (experience, skills, projects, education, certifications, achievements) and keeps each
   bullet's original wording and location ("page 1 · EXPERIENCE · bullet 2").
4. Matches each value to your existing records and writes through the normal profile services:
   - new facts are created **unverified** ("resume claim"); nothing is confirmed by an import;
   - a **confirmed** record is never changed: a different value is listed as a **conflict**, a value for an empty
     field as a **suggestion**;
   - an unverified record is refreshed only where it still holds what an earlier import wrote; if you edited it,
     your text is kept and the difference becomes a conflict;
   - kind of work, employment type, issuers and years of experience are never guessed. Dates keep their stated
     precision ("Dec 2025" is stored as 1 Dec 2025 and labelled month precision; a year-only date is kept in the
     notes, not invented).
5. Reads every written record back, then shows a report: created / updated / unchanged / conflicts / suggestions.

Re-importing the same file changes nothing. A backup (`pre-hub-import`) is taken before each real import, and a
failed import leaves the profile as it was (the failed attempt is recorded).

**Resolving**: *Needs your attention* lists conflicts and suggestions. *Use resume value* writes it into the record;
*Keep mine* marks the resume value rejected so it is not raised again for that file. Conflicts also appear in the
review queue and on application preparation pages until resolved.

**States** shown next to each value: *verified* (the record is confirmed), *resume claim* (imported, unconfirmed),
*unverified* (other unconfirmed sources, e.g. repository docs or answer drafts), *learning*, *outdated*, *rejected*.
Confirm records with the buttons in the hub; only confirmed facts are used for matching, resumes, answers and forms.

**Skills**: each skill shows the resume category it was listed under, stated versions (e.g. Spring Boot 3.x on the
skills list, 3.5 in a project), and whether it is evidenced by work, a personal project, or only listed.
Proficiency is never inferred.

**Answer drafts**: the import adds unapproved answer-bank drafts only where the answer is already stated (notice
period, relocation, the first project's and role's own bullets). Drafts are never suggested until you approve them.

**Project documentation**: `jobpilot hub link-docs --project NAME --path ~/projects/<repo>` references a local
repository's README (title and first paragraph), the names of files in `docs/`, and its `origin` URL. Source code
and other files are not read. Repository text is kept separate from the resume's wording.

## 2. Resumes

- Create the standard slots, then **import** your existing files. Files are stored byte-for-byte
  (sha256 shown); JobPilot never edits them. Each import is a new **draft** version.
- Tick *Add skills found in this file…* to record detected skills as **unverified** suggestions.
- **Approve** a version after checking it. Only approved versions are used.
- Each version shows **checks** before you approve it (and, re-run against your current profile, after):
  - changes since the previous approved version: skills added/removed, new lines with impact numbers, and a
    line diff;
  - skills in the file that are not confirmed in your profile;
  - lines with impact numbers ("reduced X by 40%"), which you should be able to support;
  - development wording ("developed", "backend engineer") under an employer whose role you recorded as
    testing or other non-development work.
  A version that changed **materially** since an approved one, or that contradicts your recorded kind of work,
  needs you to tick *I reviewed these changes* before it can be approved. What you reviewed is stored with the
  approval and in the audit log. JobPilot only flags these; it never edits your file.
- **Generate** builds a PDF from facts you select. Only confirmed facts are allowed, and the result
  is a draft until you approve it. Validation checks that the generated text contains the
  expected name and companies.
- For each job, JobPilot picks the approved resume in the job's category (best skill coverage),
  falls back to a related category or your master resume, and **tells you** when it had to fall
  back or when no approved resume exists. It never writes resume content for you.

### Tailored resumes

For every eligible job JobPilot can make a job-specific PDF from your approved Master resume's LaTeX source: the same
approved content, re-ordered for the job, compiled locally and validated, then attached to that job's application
instead of the Master resume. See [TAILORED_RESUMES.md](TAILORED_RESUMES.md) (`jobpilot resume …`, the *Tailored
resume* panel on a job's page).

## 3. Jobs

**Sources** (one company board or career site each). Every type except `jsonld`/`sitemap` accepts optional
`"title_keywords"`, `"location_keywords"` and `"exclude_keywords"` lists; only matching postings are kept.

| Type | Config | Notes |
|---|---|---|
| greenhouse | `{"board_token": "acme", "title_keywords": [...]}` | public Job Board API. With keywords, the light listing is filtered first and only matches are read (boards over 5 MB work). Applications use Greenhouse's own form (`job-boards.greenhouse.io/embed/job_app?for=…&token=…`) when the posting links to the company's site |
| lever | `{"site": "acme"}` | public Postings API |
| ashby | `{"board": "acme"}` | public Posting API; not yet tested against a live board |
| jsonld | `{"urls": ["https://…/job/1", …]}` | job pages with schema.org JobPosting data |
| sitemap | `{"sitemap_url": "…/siteMap.xml", "title_keywords": [...]}` | any site with a sitemap and JobPosting pages. On Workday it sees only the ~100 newest postings worldwide; prefer `workday` |
| workday | `{"site_url": "https://acme.wd5.myworkdayjobs.com/External", "queries": ["software engineer india"], "title_keywords": [...], "location_keywords": ["india"]}` | the site's own job search, then each kept posting's HTML description and official closing date (`endDate`) |
| amazon_jobs | `{"queries": ["software development engineer"], "country": "IND", "title_keywords": [...]}` | amazon.jobs public search |
| microsoft_careers | `{"queries": [...], "location": "India", "seniority": ["Entry", "Mid-Level"], "title_keywords": [...]}` | apply.careers.microsoft.com `/api/pcsx` (robots.txt allows it) |
| google_careers | `{"queries": [...], "location": "India", "target_level": ["EARLY"], "title_keywords": [...]}` | first results page per query only: robots.txt disallows paging. Fails loudly if Google changes its page data |
| apple_jobs | `{"queries": [...], "location": "india-INDC", "title_keywords": [...], "max_pages": 3}` | jobs.apple.com search plus each matching job's details (qualifications); one requisition listed per city is one job. Fails loudly if Apple changes its page data. Meta is not covered (its terms forbid automated collection); Netflix had no India roles when checked |
| atlassian_careers | `{"location_keywords": ["india"], "title_keywords": [...]}` | the complete listing Atlassian's careers page loads |

Not collectable (checked 2026-10-18): **JPMorgan** (Oracle careers API and even its robots.txt answer 403 from a web
application firewall: an anti-bot control, so JobPilot does not try), **Google sitemap** (none published), Lever
boards for Atlassian (Atlassian uses iCIMS; its listing endpoint is used instead).

**Add jobs manually**: paste a URL (Greenhouse/Lever links use their public APIs; others are read
for JobPosting data), import CSV/JSON, or type the details in.

Collection respects robots.txt (RFC 9309: the most specific rule wins, so `Disallow: /` plus
`Allow: /api/jobs` permits `/api/jobs`), waits ≥2 s between requests to a host, uses timeouts and bounded
retries, and records each source's last result. Jobs that disappear from a board are marked
**expired**; when the same company/title/location reappears later it's flagged as a **repost**.
Duplicates across sources are merged and every sighting is kept as provenance.

## 4. Matching

Each job gets:

- **Relevance (0–100)**: role fit (35), skills (35), level (10), location/work mode (10),
  keywords (5) and salary (5). Each line has an explanation. A title matching an exclusion keyword
  scores 0.
- **Eligibility**: `eligible`, `likely_eligible` (stated requirements met; some secondary information
  unknown), `unknown` (a key requirement or your corresponding fact is missing) or `ineligible`. It
  checks experience, required skills ("X or Y" counts once), location, sponsorship/authorisation
  and degree mentions.

Jobs that are relevant but have unknown eligibility, or that score in the borderline band, go to
the **Review queue**. After changing your profile or settings, click *Re-score all*.

## 5. Contacts and email drafts

- Record recruiters, hiring managers and referral contacts with **where you found them**. Email
  status is `verified` (needs a note on how), `unverified` or `guessed`. Guessed addresses are always
  labelled and need an explicit acknowledgement before approval.
- *Record as contact* on a job turns addresses published in the posting into unverified contacts.
- The **do-not-contact** list (email, domain or company) blocks drafting, approval, sending and
  follow-ups.
- Drafts come from six templates (outreach, hiring manager, referral, follow-up, thank-you, status
  inquiry). Unknown facts render as `[[MISSING: …]]`. Replace them yourself before you can approve.
  Templates make no claims about relationships or conversations.
- Workflow: edit → **Approve** → send it yourself (Copy button) → *Mark as sent*, or send through
  Gmail if connected. A follow-up date is set (default 7 days, max 2 follow-ups). Recording a reply,
  marking the contact do-not-contact, or closing the application suppresses follow-ups.

## 6. Applications

From a job page click **Start application**. The pipeline:

`discovered → shortlisted → preparing → ready_for_review → applied → assessment → interview → offer`
(plus `rejected` / `withdrawn`).

- *Preparing* selects the resume (changeable) and builds a checklist. Required items must be ticked
  before *ready for review*.
- The application page shows the **next action**. The preparation record shows the stated deadline (only
  from source data or an explicit "apply by" sentence, never guessed) and the selected resume's checks
  (job skills it lists that you haven't confirmed, contradictions with your recorded work). Experience
  evidence is labelled by kind of work and project kind.
- **Reminders** (scheduled task `reminders`, or *run now* on System) raise review items, never messages:
  a stated deadline within `deadline_warning_days` (default 3) for shortlisted jobs and open applications;
  a submitted application with no recorded update for `stale_application_days` (default 21), worded as
  "check on it", **never** as a rejection; and a listing that expired while an application is open (an
  expired listing is not a rejection either). Each reminder is raised once per fact, so a dismissed
  reminder stays dismissed until, say, the deadline changes.
- **Duplicates** (same job, same apply URL, same company+title within 120 days, or a repost of a job
  you applied to) are blocked unless you record why it's different.
- **Submission**: click *Approve submission*, submit on the employer's site yourself, add
  confirmation evidence (confirmation email text, confirmation page, screenshot or portal status),
  tick the attestation, then *Record submission*. Only then is the status `applied`, and the resume
  version is frozen.
- **Browser assist** (optional, `JOBPILOT_BROWSER_ENABLED=true`): opens the form in Chrome with a
  separate JobPilot profile. On Greenhouse and Lever forms it fills name, email, phone, location and
  links from confirmed facts, your **approved answers** (this application's, then close matches from the approved,
  non-sensitive answer bank; drop-downs only when one option equals the answer exactly) and attaches the resume. It
  **pauses** on CAPTCHA, login, MFA, consent, legal/EEO questions and unknown questions, and it **never clicks
  submit**: you review and click Submit yourself. If a confirmation page appears, its text and a screenshot are saved
  as candidate evidence. Other sites: the page opens and you use the manual workflow (copyable confirmed details on
  the application page). This is the way through forms with a bot check (all Greenhouse boards checked).
- JobPilot never submits applications for you: the automatic-application feature (autopilot) and its form rehearsal
  were removed on 2026-10-23.

### My tracker (applications you make yourself)

**My tracker** (`/tracker`, or Telegram `/alljobs`, `/track`, `/applied`, `/company`, `/dream`) lists every job
JobPilot found with its apply link — relevant, not-ineligible ones by default, everything with *include ineligible* —
dream companies (⭐, `tracker.dream_companies`, FAANG by default) first. When you've applied yourself, mark it
**✅ applied** with the resume you used and the date: the application is recorded as *applied* with confirmation method
`self_reported` (your statement is the evidence). **⏳ not yet** asks again after `tracker.ask_again_days`;
**🚫 not interested** dismisses the job.

*Applications by company* shows each company's applications (status, date, resume), your last application there and
its cooldown. Search by company name. Cooldowns are yours to set (`/cooldown amazon 90` or the form): companies rarely
publish one, so none is assumed; with one set, the company shows *wait until <date>* or *ok to apply*.

Employer emails then keep the status current (Gmail sync, headers only): a clear rejection / assessment / interview /
offer subject updates the application by itself (undo on the tracker or the Telegram **↩️ Undo**); an unclear one is
asked about on the tracker and in Telegram. Settings: `tracker` (dream companies, cooldowns, prompts per day, days
before asking again, automatic outcomes on/off).

## 7. Interviews

Add interviews to submitted applications. Paste an invitation email to extract date/time, meeting
or assessment link and round type. This creates a **suggestion** you check and confirm. With Gmail
body access enabled, *Scan for interview invitations* does the same for recent mail. **Generate
preparation pack** produces a stage-specific plan: DSA, Java, Spring Boot, SQL, LLD, HLD,
behavioural, Python and ML topics as relevant, plus REST/backend and testing topics for backend jobs; job
skills you have, are learning, or are missing; STAR prompts from your confirmed experience, labelled
so testing work is not told as development and personal projects are not told as production work;
questions about your own confirmed projects; and questions to ask. Record outcomes, feedback and follow-ups;
draft a thank-you note.

## 8. Skill gaps

**Skill gaps** lists skills your relevant/shortlisted/applied jobs require that you haven't
confirmed. The **learning checklist** orders them by demand in those jobs, weighted up for interview
relevance and down for rough effort (days / weeks / months, a generic estimate for the skill's category). *Add to backlog* marks the skill as learning.
Track status/progress (self-reported, capped at 95%). To claim the skill, *Confirm skill* with
concrete evidence. Nothing is marked mastered automatically.

## 9. Analytics

Jobs collected, relevant share, applications by status, response/positive-response/interview/offer
rates, source effectiveness and resume performance. Rates need at least 5 records in the
denominator; smaller samples show raw counts as "insufficient data". Missing data (no salary, no
posting date…) is counted separately and never treated as zero. CSV exports: jobs, applications,
contacts, drafts, interviews, summary.

## 10. System

Health checks, backups (create/download/restore; restore saves the current state first), task
runs, scheduled tasks (*run now*), structured logs with level filters, the audit log, full JSON
export, and data deletion by scope.

Scheduled tasks (all off until you enable them under Settings → `scheduler`): `collect_jobs`,
`maintenance` (expiry from stated deadlines), `rematch` (re-scores only unscored jobs or jobs scored before
your profile/settings changed), `reminders`, `followup_check`, `backup`, `health_check` (includes an SQLite
integrity check), `gmail_sync`. A failed task is retried after 15 min, then 30 min, 1 h, …, capped at its
own interval, so a source that is down is not hit on every pass.

`jobpilot doctor` prints the same health information from the command line without changing anything
(it never migrates).
