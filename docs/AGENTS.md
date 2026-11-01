# Agents: Search, Resume, Apply and the Manager

Four agents do the job search and applying for you. They are rule-based (no AI model, no API key, nothing leaves
your laptop except the requests each step needs). The dashboard page is **Agents** (`/agents`).

| Agent | What it does each cycle | Code |
|---|---|---|
| **Manager** | Picks the jobs, runs the other three in order, keeps the daily limit and the pause switch, sends you a Telegram summary | `agents/manager.py` |
| **Search** | Runs every source; adds the public feeds (Remote OK, Working Nomads, We Work Remotely, Himalayas, Jobicy, Y Combinator) and the recommended company boards; reads LinkedIn / Naukri / Indeed / Glassdoor / foundit / Instahyre **alert emails** in your Gmail; finds the official Greenhouse / Lever boards of companies seen only in feeds and alerts and adds them as sources | `agents/search.py`, `sources/feeds.py`, `services/job_alerts.py`, `services/board_discovery.py` |
| **Resume** | A tailored, validated PDF for each picked job: re-ordered for the job, skills spelled the way the posting spells them, all 13 checks | `agents/resume.py`, `services/tailored.py`, `services/wording.py` |
| **Apply** | Emails the application when the posting asks for that; submits Greenhouse / Lever / Ashby forms when it is safe; hands everything else to you, ready to go | `agents/apply.py`, `automation/apply.py` (`submit_page`) |

## Starting it (once)

1. **Agents page → Next cycle → Turn on** (every 3 h by default). Or `jobpilot agents run` for one cycle now.
2. Leave **Apply mode** on **Dry run** for the first cycles. Dry run does everything (search, resumes, contacts,
   email drafts, opening and checking forms) but sends and submits nothing. Look at **What each agent did last**
   and **Needs you**.
3. When you're happy: tick **send real applications** and **Switch to automatic**
   (or `jobpilot agents mode auto --yes`).
4. For alert emails: set `JOBPILOT_GMAIL_ALLOW_READ_BODIES=true` in `.env`, run `jobpilot gmail auth` again, and
   restart the service. Without it the Search agent skips alert emails and says so.

## How a job is picked (Settings → agents, or the Policy box on the Agents page)

Active, seen in the last `max_job_age_days` (30), score ≥ `min_score` (70), eligibility in
`allowed_eligibility` (eligible, likely eligible), no hard conflict, job family in `job_families` (empty = any),
not dismissed, not already applied to, not waiting on you, not at a company in its cooldown (My tracker → cooldowns).
Best score first, at most `max_per_cycle` (5) per cycle and `daily_limit` (10) per day.
Dream companies (My tracker) are prepared for you instead (`hold_dream_companies`): resume made, link ready.

## How it applies

1. **By email**, when the posting says so ("send your CV to jobs@acme.com"). The address must appear in a sentence
   asking for applications; privacy, legal, support and no-reply addresses are never used. The address is recorded
   as a contact, verified by the posting (the sentence is quoted on the contact). The email is short and built only
   from confirmed facts (your name, current role, the job's skills you have, contact details and links), with the
   tailored resume attached. It goes through the normal Gmail send path: blocked and bounced addresses, the
   outreach daily send limit, and no retry after an unknown outcome. One application email per job, ever.
2. **By form** on Greenhouse, Lever and Ashby. The agent fills the form from confirmed facts, your approved answers
   and the answer bank, attaches the tailored resume, and clicks Submit **only if** the page has no CAPTCHA or bot
   check (visible or invisible), login or verification prompt; every required field has an approved value (a
   required consent or legal box is never ticked, so those forms always come to you); and there is exactly one
   Submit button. Every condition is checked again right before the click, and the attempt is saved before the
   click. Outcome: *confirmed* (the confirmation page is saved as evidence and the application is `applied`),
   *rejected by the form* (comes to you), or *unclear* (comes to you; never submitted again).
3. **Everything else comes to you** on **Needs you**, with the one thing to do:
   - a bot check or login: `jobpilot assist <id> --approve-host <host>` opens the form filled; you complete the check
     and click Submit;
   - unanswered questions: answer them on the application page (or `/answer` in Telegram); the next cycle continues;
   - Workday, company career sites, Jobicy / Remote OK listings: apply on the site with the ready resume, then
     **✓ I applied** on the job page.

**What to expect.** In October 2026 every Greenhouse board checked loaded an invisible reCAPTCHA, Lever showed a
visible CAPTCHA, and Workday, Amazon, Google and Microsoft need an account login. So most form applications
end up as "one step for you"; email applications and the occasional form without a bot check go out on their own.
JobPilot does not get past bot checks or logins, by design.

## Controls

| Where | What |
|---|---|
| Agents page | Run a cycle now (or one agent), switch dry run / automatic, pause, schedule, policy |
| Telegram `/pause`, `/resume` | One switch for all sending: agents and recruiter outreach |
| `jobpilot agents run [--only search\|resume\|apply] [--mode dry_run\|auto --yes]` | One cycle now |
| `jobpilot agents status` | What each agent did last |
| `jobpilot agents mode dry_run\|auto --yes`, `jobpilot agents pause\|resume` | Same switches from the terminal |

## What the agents never do

Get past a CAPTCHA, bot check, login or verification code; tick a consent, privacy, NDA or legal box; answer a
question without your approved answer; claim a skill or fact you haven't confirmed; invent resume content (only
re-ordering, confirmed corrections and same-meaning skill names); submit an application twice; scrape LinkedIn,
Naukri or Indeed (only the alert emails they send you are read).
