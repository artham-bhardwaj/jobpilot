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

## 3. Jobs

**Sources** (one company board each):

| Type | Config | Notes |
|---|---|---|
| greenhouse | `{"board_token": "acme"}` | from `boards.greenhouse.io/acme` |
| lever | `{"site": "acme"}` | from `jobs.lever.co/acme` |
| ashby | `{"board": "acme"}` | from `jobs.ashbyhq.com/acme`; not yet tested against a live board |
| jsonld | `{"urls": ["https://…/job/1", …]}` | job pages with schema.org JobPosting data |

**Add jobs manually**: paste a URL (Greenhouse/Lever links use their public APIs; others are read
for JobPosting data), import CSV/JSON, or type the details in.

Collection respects robots.txt, waits ≥2 s between requests to a host, uses timeouts and bounded
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
  links from confirmed facts and attaches the resume. It **pauses** on CAPTCHA, login, MFA, consent,
  legal/EEO questions and unknown questions, and it **never clicks submit**. If a confirmation page
  appears, its text and a screenshot are saved as candidate evidence. Other sites: the page opens
  and you use the manual workflow (copyable confirmed details on the application page).

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
