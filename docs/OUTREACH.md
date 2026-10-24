# Recruiter contacts and outreach

JobPilot helps you record the right recruiter for a job and write a short, factual email. It **never sends email**:
drafts go to your Gmail Drafts and you send them yourself. It **never visits, scrapes or messages LinkedIn**: profile
URLs are what you paste in.

## 1. Add a recruiter

Found someone on LinkedIn (or the company site):

```bash
jobpilot recruiters add --job 131 --name "Jane Recruiter" --title "Technical Recruiter" --company Rubrik \
  --linkedin https://www.linkedin.com/in/jane-recruiter --source linkedin
```

Found their work email in Apollo's own web app (manual mode, no API, no credits through JobPilot):

```bash
jobpilot recruiters add --job 131 --name "Jane Recruiter" --company Rubrik --email jane@rubrik.com --source apollo
# or import an Apollo CSV export (First Name, Last Name, Title, Company, Email, Email Status, Person Linkedin Url)
jobpilot recruiters import ~/Downloads/apollo-contacts.csv --job 131
```

Email status means exactly what is known:

| Status | Meaning |
|---|---|
| `verified` | **you** confirmed it (they replied from it, it is on the company site); needs `--verification-note` |
| `apollo_reported` | Apollo says its check passed; JobPilot has not confirmed it |
| `unverified` | published or obtained, not confirmed |
| `guessed` | inferred; never sent to without your explicit acknowledgement |
| `bounced` | mail to it came back; blocked for all outreach |

Contacts are deduplicated by email and profile URL (and name + company); merging never overwrites what is already
recorded. A personal address (gmail.com, outlook.com…) from an import is not stored. If the email domain does not fit
the company, you get a warning (Telegram, dashboard, draft page); nothing is changed silently.

## 2. Optional: Apollo API

Off by default. Needs **both** `JOBPILOT_APOLLO_API_KEY` in `.env` and **Settings → apollo → `"enabled": true`**.

- `jobpilot recruiters search --domain rubrik.com` — People API Search, **0 credits**: recruiter candidates (last
  names obfuscated, no emails). Nothing is stored.
- `jobpilot recruiters enrich --apollo-id ID --job 131 --spend-credit` — People Enrichment for one person: their work
  email and Apollo's status, **about 1 credit when an email is returned**. Refused without `--spend-credit` and once
  `apollo.max_enrichments_per_day` (default **0**) is reached. Personal emails and phone numbers are never requested.

## 3. Write the email

```bash
jobpilot outreach draft --job 131 --contact 4      # local draft; shows text, missing facts and warnings
jobpilot outreach approve --draft 7                # your approval of the text and recipient
jobpilot gmail create-draft --draft 7              # into Gmail Drafts — not sent
```

The text uses only confirmed facts: the job title and URL, the recruiter's name, your current role and stated years,
skills the posting asks for that your profile confirms, and at most one **personal** project that uses one of those
skills. It says "I applied on …" only when an application is recorded as applied. Unknown facts appear as
`[[MISSING: …]]` and block approval.

Duplicate protection: one outreach message per recruiter per job; a second recruiter for the same job only with
`--reason`; do-not-contact, suppressed and bounced addresses get no drafts; repeating `gmail create-draft` reuses the
same Gmail draft. Follow-ups are reminders only (Telegram, dashboard); they are never sent for you.

## 3b. Automatic outreach (scheduler task `outreach`, every 6 h)

Settings → outreach: `auto_enabled` (queue discovery, write emails) and `auto_send` (also send), `auto_daily_limit`
(default 3, within `daily_send_limit`), `max_job_age_days`. For each job that passes the outreach filter (`min_score` 70, `allowed_eligibility` eligible / likely eligible,
`job_families`, no hard conflict, not dismissed):

| Situation | What happens |
|---|---|
| a recruiter email for this job already exists (draft / approved / sent) | nothing — one per job, never a second |
| no recruiter contact at this employer linked to the job | queued: *Find a recruiter* (Telegram 🔍), with the exact `jobpilot recruiters add --job …` command |
| contact's address is `apollo_reported` / `unverified` / `guessed` | queued: *Verify the email*; nothing written or sent |
| verified by you, but facts missing or the domain doesn't fit the employer | draft written, queued for your review; not sent |
| verified by you, everything in order, `auto_send` on, under the daily cap | approved under your standing authorization and **sent** via Gmail (same checks as a manual send) |
| over the daily cap or `auto_send` off | put in Gmail Drafts for you |

Only emails this task wrote in the same run are ever sent: older or cancelled drafts never are. Bounced, suppressed,
do-not-contact and placeholder contacts are skipped. A failed send is reported (🔍 alert) and not marked sent; an
unknown outcome is never retried. A "📧 Recruiter email SENT" alert appears only when Gmail returned the sent
message id. Follow-ups are never sent automatically. `/pause` stops this task too.

## 4. Replies and bounces

The `gmail_sync` task (every 6 h, headers only) marks a reply when a message that is not yours arrives in the thread
of a message you sent, and stops follow-ups. A delivery-failure notice (mailer-daemon/postmaster, "Delivery Status
Notification (Failure)") is a **bounce**, not a reply: the address is marked bounced and blocked. Message bodies are
not read, so a reply is reported as "reply received" — open Gmail to read it.

## 5. Telegram

`/recruiters`, `/outreach`, `/outreach_pending`, `/outreach_stats`; alerts for recruiter contacts added, recruiter
emails placed in Gmail Drafts (not sent), and bounces. Contact details are only ever sent to your private chat.
