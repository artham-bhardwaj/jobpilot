# How to use JobPilot

JobPilot runs on your laptop as a background service. It finds jobs **in India, remote jobs open to India or the UAE,
and jobs in Dubai / UAE**, scores them against your **confirmed** profile, makes a tailored resume for any job when you
ask, tracks every application and watches your inbox for replies. **It never applies for you**: you open each posting
and apply yourself.

The dashboard is at **http://127.0.0.1:8765**; that's where you work. Telegram (**@ArthamJobPilotBot**) only sends
alerts: high matches, application updates, failures and the daily summary.

Three rules it never breaks:
- It never invents facts. Only confirmed profile information appears in scores, resumes, emails and forms.
- It never gets past a CAPTCHA, login, MFA or anti-bot check, and never accepts a legal agreement, consent or
  signature for you.
- It never submits an application. The browser helper can fill a form, but you click Submit.

---

## 1. Daily routine (5 minutes)

1. **Overview** (`/`). **Ready to apply** lists your best relevant jobs that you haven't applied to yet, each with
   **apply ↗** and **record / resume**. **Jobs by region** shows India, Remote and Dubai / UAE below it.
2. **Jobs** (`/jobs`). Pick a region tab; set **Applied?** to *not applied yet* to hide jobs you've done. Applied jobs
   are tinted green with the date. Open a job to read its score, eligibility and description.
3. On the job's page, the **apply panel** at the top has the three steps:
   1. **Resume**: download the tailored PDF if you made one, or the recommended resume (**create one for this job**
      if you want it tailored, see §5; nothing is generated unless you ask).
   2. **Apply on company site ↗**.
   3. **Record it**: pick the resume you used (the tailored one is pre-selected when it exists), the date, and click
      **✓ I applied**. Or **Save for later** / **Not interested**.
   Re-score, check still open, mark expired and triage are under **More actions**.

**My tracker** (`/tracker`) still lists every job with its apply link, your applications by company and cooldowns.

Job collection, scoring, email checks and backups run on their own. Telegram tells you about strong matches and
application updates, so you don't have to check the dashboard all day.

---

## 2. Starting, stopping, checking

JobPilot runs as the systemd user service `jobpilot.service`. It starts when you log in.

```bash
systemctl --user status jobpilot      # running?
systemctl --user restart jobpilot     # after changing .env or updating the code
systemctl --user stop jobpilot        # stop everything (dashboard, scheduler, Telegram bot)
journalctl --user -u jobpilot -f      # live log
```

Commands run from the project folder: `cd ~/projects/jobpilot`, then `.venv/bin/jobpilot …`.

| Command | What it tells you |
|---|---|
| `jobpilot doctor` | paths, database revision, health and integrity |
| `jobpilot scheduler status` | each scheduled task, when it last ran, when it runs next |
| `/status` in Telegram | the same, from your phone |

The laptop must be on (and logged in) for anything to run. Missed tasks run at the next pass after it wakes up.

---

## 3. The dashboard

| Page | Use it for |
|---|---|
| **Overview** `/` | **Ready to apply** (best jobs you haven't applied to), today's numbers, the next thing to do, and **Jobs by region** (top matches in India, Remote, Dubai / UAE) |
| **Jobs** `/jobs` | every collected job, with **region tabs** (All · India · Remote · Dubai / UAE · Elsewhere) and region badges; filter, sort, shortlist, dismiss, open the posting, or go to the job's resume. A job's page shows its score, eligibility, skills, the resume chosen for it, the **Resume for this job** panel (create, download, decisions, regenerate), contacts and the description |
| **Matching** `/matching` | why jobs score as they do; thresholds |
| **Review queue** `/review` | everything waiting for you, in one list |
| **Applications** `/applications` | the pipeline by status; each application's checklist, questions, evidence and history |
| **My tracker** `/tracker` | every job with its apply link (⭐ dream companies first); mark applied (resume and date), not yet, or not interested; applications **by company** with cooldowns; search by company |
| **Answer bank** `/answers` | approved answers reused on forms (never legal or sensitive ones) |
| **Interviews** `/interviews` | interviews, prep packs, learning items |
| **Information hub** `/hub` | import your resume into your profile, with provenance |
| **Profile** `/profile` | your facts: employment, education, projects, skills, certifications. **Only confirmed facts are used** |
| **Resumes** `/resumes` | your resume versions: upload, review, approve, archive |
| **Skill gaps** `/skills` | skills jobs ask for that your profile doesn't confirm |
| **Recruiters** `/contacts`, **Companies**, **Email drafts** | recruiter contacts, outreach emails and their real state |
| **Analytics** `/analytics` | response rates by source, resume and company |
| **Sources** `/sources` | where jobs come from; **Add regional sources** (India / remote / Dubai); run one now; see failures |
| **Settings** `/settings` | thresholds, notifications, scheduler, outreach, tracker, tailored resumes |
| **System** `/system` | health, backups, logs, task runs, pause automatic outreach |

---

## 4. Telegram (alerts only)

Telegram is for alerts; everything else is in the dashboard. Send `/menu` for buttons. Only your own chat is
answered.

| Command | What it does |
|---|---|
| `/status`, `/today` | health and schedules; today's summary |
| `/jobs` | top relevant jobs, with eligibility (✅ eligible / ❓ check / ⛔ ineligible) and region |
| `/alljobs`, `/dream` | every relevant job with its apply link; jobs at your dream companies |
| `/track`, `/applied`, `/company amazon`, `/cooldown amazon 90` | record applications and cooldowns from your phone |
| `/applications`, `/outcomes` | pipeline by status; recent status changes |
| `/questions`, `/answer Q12 your answer` | questions from application forms; answer one (add `+bank` to reuse it later) |
| `/recruiters`, `/outreach`, `/outreach_pending`, `/outreach_stats` | recruiter contacts and emails |
| `/pause`, `/resume` | stop or restart automatic recruiter outreach |

What arrives on its own (default):
- 🎯 high matches;
- 📬 employer emails: a clear rejection, assessment, interview or offer updates the application, with **↩️ Undo**,
  and anything unclear is asked about;
- 📋 application status changes, replies, follow-ups due, sent and bounced recruiter emails;
- ⚠️ failed tasks or sources;
- 📊 the daily summary.

Off by default (the dashboard shows them): the new-jobs digest, "Did you apply?" questions, form questions, draft
reviews and recruiter tasks. Turn any kind on in **Settings → notifications**.

---

## 5. Resumes

### Your Master resume

The Master resume is the approved PDF everything starts from. The active version is **Master resume v2**, the same as
v1 with the Class XII year corrected to 2020. v1 is kept, still approved, and still used by the applications that were
prepared with it.

- v1 PDF: `data/files/resumes/8da104a95abc59dd_ArthamBhardwajBackendEngineer2.pdf`; source:
  `~/Downloads/twopage.tex`.
- v2 PDF: `data/files/resumes/9824ec0a37a33a7e_ArthamBhardwaj_Master_v2.pdf`; source:
  `data/files/resume_sources/ArthamBhardwaj_Master_v2.tex`.

Every upload is checked against the approved file's checksum. A changed or corrupted file is never attached.

**To change your resume:**
1. Copy the current source to a new file and edit it.
2. Compile it. `latexmk -pdf file.tex` works, or let JobPilot do it.
3. **Resumes → Master resume → upload** the PDF as a new version, review the comparison, and approve it.
4. `jobpilot resume template --file your_new_source.tex`.

From then on, tailored resumes come from the new version. Old ones are marked *outdated*; create a new one on the job's
page when you need it. Applications already approved keep the resume they were approved with.

### Tailored resumes (for any job, when you ask)

On a job's page, **Create resume for this job** makes a PDF from your Master resume's LaTeX source. It's available for
every job (a low score, a short description or an ineligible job only adds a note), and **nothing is generated in the
background**. It is built locally, with no internet and no AI.

**What changes:**
- Bullets, projects and the skills in each line are re-ordered so the ones that match the job come first.
- Education years come from your confirmed records.

**What never changes:**
- Wording, numbers, employers, titles and dates are never changed.
- Nothing is added: skills a job wants that you don't have are listed on the job's page, never claimed.

**Before a PDF is used:**
- It must pass 13 checks: same content as the Master, nothing clipped, no blank pages, page limit, and others.
- It is attached only to the application for that same job, and only while it's current.
- Otherwise the approved Master resume is attached.

| Command | What it does |
|---|---|
| `jobpilot resume preview --job 126` | what would be re-ordered and why; nothing saved |
| `jobpilot resume generate --job 126` | make (or reuse) and validate |
| `jobpilot resume path --job 126` | path of the validated PDF, to attach yourself |
| `jobpilot resume show --job 126` | decisions, fact mapping, every check |
| `jobpilot resume validate --job 126` | re-check a stored PDF |
| `jobpilot resume list` | all tailored resumes and their status |

Exit codes: 0 done, 1 bad input, 3 failed (the reason is printed), 4 no LaTeX compiler. Details:
[docs/TAILORED_RESUMES.md](docs/TAILORED_RESUMES.md).

---

## 6. Applying

JobPilot never applies for you; the automatic-application feature (autopilot) was removed on 2026-10-23.

### Yourself (every job)

1. Open the job (**open posting** on the Jobs list, the apply link on **My tracker**, or Telegram **🔗 Apply**).
2. If you want a tailored resume, click **Create resume for this job** on its page and download it (or
   `jobpilot resume path --job ID`).
3. Apply on the company's site.
4. Record it on **My tracker** (or tap **✅ I applied** in Telegram) and pick the resume. The application is recorded
   with *self-reported* evidence, and JobPilot starts watching your inbox for that company.

### Assisted (Greenhouse forms)

```bash
jobpilot assist <application id> --approve-host job-boards.greenhouse.io
```

A browser opens with the form filled from your confirmed facts and approved answers, and the right resume attached.
It stops at anything only you can do (CAPTCHA, legal questions, unknown questions). **You review and click Submit.**
Afterwards, record the submission on the application's page, or let the confirmation email record it automatically.

### Cooldowns and duplicates

- **Duplicates:** JobPilot warns before a duplicate application (same job, same URL, or same company and title within
  the window).
- **Cooldowns:** set a company's waiting period with `/cooldown <company> <days>` or on **My tracker**. The company
  then shows *wait until <date>* or *ok to apply*.

---

## 7. Tracking applications

Employer emails are read from **headers only** (sender, subject, date); bodies are never read.

| Email | What happens |
|---|---|
| "Thank you for applying" | records a submission JobPilot opened the form for |
| Clear rejection, assessment, interview or offer | updates the status, with **↩️ Undo** |
| Anything else from that company about an application | you're asked |

Your applications appear under:
- `/applied` and **My tracker**: by company, with resume and dates;
- **Applications**: the pipeline, with a full history per application.

Statuses: discovered → shortlisted → preparing → ready for review → applied → assessment → interview → offer, or
rejected / withdrawn.

---

## 8. Recruiter outreach

| What | How |
|---|---|
| Add a recruiter you found (LinkedIn or Apollo, by hand) | **Recruiters → add**, or `jobpilot recruiters add --name … --company … --email … --email-status verified` |
| Import an Apollo CSV | `jobpilot recruiters import file.csv` |
| Apollo API search (free) and enrich (uses a credit, off unless you allow it) | `jobpilot recruiters search` / `enrich` |
| Draft, approve, cancel, list emails | `jobpilot outreach draft|approve|cancel|list`, or the **Email drafts** page |

Automatic outreach is **on** (pause it with `/pause` or on the **System** page):
- **Limits:** at most 3 emails a day, only to addresses you marked *verified*, one email per recruiter per job.
- **Never automatic:** no follow-ups, and nothing to do-not-contact or bounced addresses.

Without a verified contact, a job becomes a "recruiter needed" task in Telegram. JobPilot never scrapes or automates
LinkedIn. More: [docs/OUTREACH.md](docs/OUTREACH.md).

---

## 9. Jobs and sources

Every job is tagged with its region from its location and title:
- **India:** any Indian city or state, or "India".
- **Remote:** a remote job open to you: "Remote", "Remote - India", "APAC", "EMEA", "Worldwide". A remote job limited
  to another country ("Remote - US") is **Elsewhere**, and its location check warns you.
- **Dubai / UAE:** Dubai, Abu Dhabi, Sharjah, UAE.

A job can be in two regions ("Remote - India" is India and Remote).

The sources:
- **India:** Amazon, Apple, Google, Microsoft, Atlassian; Workday (Visa, Mastercard, Adobe); Greenhouse and Lever
  boards (Rubrik, GitLab, Stripe, MongoDB and others).
- **Remote:** Himalayas (remote jobs open to India or the UAE) and Jobicy (remote jobs worldwide, APAC, EMEA, UAE).
- **Dubai / UAE:** Bybit, Binance, OKX, Careem (Greenhouse), Amazon UAE, Microsoft UAE. Most UAE
  engineering roles on these boards are senior, so expect few matches at your level.
- Board sources with a location filter also keep remote and Dubai roles (`"regions": ["india", "remote", "dubai"]`).

**Sources → Add regional sources** (or `jobpilot regional-sources --apply`) adds any of these that are missing. It
never changes a source you already have, except to add the regions to board sources that filter by location.

Each source respects the site's `robots.txt` and rate limits.

| Task | Command or page |
|---|---|
| Add a source | **Sources** page (config examples in [docs/USER_GUIDE.md](docs/USER_GUIDE.md)) |
| Collect now | `jobpilot collect` (`--source ID`, `--dry-run` to preview) |
| Add one posting | `jobpilot add-url <url>` |
| Import a CSV or JSON file | `jobpilot import-jobs file` |
| Re-score after profile or settings changes | `jobpilot rematch` (also runs daily) |

Scores: relevant ≥ 55, strong ≥ 75. Eligibility compares the job's stated requirements with your confirmed facts.
Your 1.0 year of experience, from TCS since 26 Dec 2025, is why many roles show ⛔.

---

## 10. Scheduler

All tasks run inside the service (`jobpilot scheduler status`):

| Task | Every | Does |
|---|---|---|
| collect_jobs | 24 h | collect from enabled sources |
| rematch | 24 h | re-score changed or unscored jobs |
| outreach | 6 h | recruiter tasks and emails to verified addresses (within the limits above) |
| gmail_sync | 6 h | replies, bounces, confirmations, employer outcomes (headers only) |
| reminders | 12 h | deadlines and stale applications |
| followup_check | 24 h | follow-ups due (never sent automatically) |
| maintenance, health_check, backup | 24 h | expiry, integrity, daily backup |
| notify | 15 min | Telegram messages |

Change intervals or turn tasks off in **Settings → scheduler**. Run one now with `jobpilot scheduler run --task NAME`.

---

## 11. Backups and your data

Everything lives in `data/`, which is kept out of git: the database, files, backups, logs and secrets.

| Task | Command |
|---|---|
| Back up (also done daily, and before every migration) | `jobpilot backup create --label why` |
| List backups | `jobpilot backup list` |
| Restore (the current state is backed up first) | `jobpilot backup restore --name <file>` |
| Export | `jobpilot export all-json` (or `jobs`, `applications`, `contacts`, …) |
| Delete personal data | `jobpilot delete-data profile|contacts|outreach|everything` |

Secrets (Telegram token, Gmail OAuth token, Apollo key) are in `.env` and `data/secrets/`. They are never printed or
logged.

---

## 12. Troubleshooting

| Symptom | Do this |
|---|---|
| Dashboard doesn't load | `systemctl --user status jobpilot`; `journalctl --user -u jobpilot -n 50` |
| No Telegram messages | `/status`; `jobpilot telegram check`; **Settings → notifications** |
| Telegram buttons look old | `jobpilot telegram menu` (re-registers commands and the keyboard) |
| Gmail "not ready" | `jobpilot gmail status`; re-authorise with `jobpilot gmail auth` ([docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md)) |
| A source fails | **Sources** page shows the error; existing jobs are kept |
| Tailored resume failed | `jobpilot resume show --job ID` (the reason); the Master resume is used meanwhile |
| "No LaTeX compiler" | `sudo apt install latexmk texlive-latex-recommended texlive-fonts-recommended` |
| A form needs a login or CAPTCHA | apply on the site yourself; `jobpilot assist …` can still fill Greenhouse/Lever forms around it |
| No Dubai or remote jobs | **Sources → Add regional sources**, then run them; check each source's last run |
| Something looks wrong | `jobpilot doctor`; restore a backup if needed |

More detail: [docs/USER_GUIDE.md](docs/USER_GUIDE.md),
[docs/TELEGRAM.md](docs/TELEGRAM.md), [docs/OUTREACH.md](docs/OUTREACH.md),
[docs/TAILORED_RESUMES.md](docs/TAILORED_RESUMES.md), [docs/SECURITY.md](docs/SECURITY.md).
