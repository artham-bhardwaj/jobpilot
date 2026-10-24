# Telegram notifications and commands

JobPilot can message you on Telegram (free Bot API, outbound HTTPS only) and answer a few **read-only** commands.
It needs no public address, webhook or paid service: commands are fetched by long polling while JobPilot runs.

What it never does: send emails or applications, approve drafts, change data, run shell commands, or answer anyone
except your configured chat.

## Setup

1. Create a bot with **@BotFather** (`/newbot`) and copy the token.
2. Add to `.env` in the JobPilot directory (git-ignored; never commit it):
   ```
   JOBPILOT_TELEGRAM_ENABLED=true
   JOBPILOT_TELEGRAM_BOT_TOKEN=<the token from BotFather>
   ```
3. Check the token (read-only; prints the bot's username, never the token):
   `.venv/bin/jobpilot telegram check`
4. In Telegram, open your bot and press **Start** (or send any message).
5. Find your chat id: `.venv/bin/jobpilot telegram find-chat` lists the chats that messaged the bot.
6. Configure **your** chat id explicitly: `.venv/bin/jobpilot telegram set-chat <chat_id>`.
   This only appends `JOBPILOT_TELEGRAM_CHAT_ID=<id>` to `.env`. It refuses ids that haven't messaged the bot, and
   never changes an existing value (edit `.env` yourself for that). No chat is ever authorised automatically.
7. Send one test message: `.venv/bin/jobpilot telegram test` (prints Telegram's message id).
8. Restart `jobpilot serve` so it picks up the chat id and starts answering commands.
9. Turn on scheduled notifications: **Settings → scheduler** → `"notify": {"enabled": true, "every_hours": 0.25}`,
   and run a scheduler (see "Scheduling" in the README).

`jobpilot telegram status` shows what is configured without contacting Telegram.

## Commands (your chat only)

| Command | Answer |
|---|---|
| `/start`, `/help` | the command list |
| `/status` | scheduler mode, enabled tasks, last successful collection, Gmail readiness, notification counts, recent failures |
| `/today` | today's summary (same as the daily summary) |
| `/jobs` | top 10 active jobs at or above your relevance threshold |
| `/pending` | drafts to review, approved drafts not sent, applications waiting for your approval, follow-ups due |
| `/applications` | applications by status, and the latest ones |
| `/outcomes` | recent browser-assisted attempts and recent application status changes |
| `/questions` | application questions waiting for your answer, with their numbers (🔒 = personal/legal) |
| `/answer Q12 your answer` | answers question 12 for that application, approved as yours. Add ` +bank` to also save a non-sensitive answer to the approved answer bank for future forms. Nothing is submitted |
| `/recruiters` | recruiter contacts: company, linked jobs, email and its actual status (see [OUTREACH.md](OUTREACH.md)) |
| `/outreach`, `/outreach_pending`, `/outreach_stats` | recruiter emails and their real state (draft / approved / in Gmail Drafts — not sent / sent / replied / bounced), what needs you next, and counts |
| `/alljobs` | every relevant, not-ineligible active job with its apply link (⭐ dream companies first) and whether you have an application there |
| `/track` | "Did you apply?" for up to 3 jobs you haven't told JobPilot about, with buttons: **✅ I applied** (then pick the resume you used), **⏳ Not yet** (asked again after `tracker.ask_again_days`), **🚫 Not interested** (the job is dismissed) |
| `/applied` | your applications grouped by company: status, date applied, resume used, last application there and cooldown |
| `/company amazon` | one company: your applications there, its cooldown and when you can apply again, and its open relevant jobs |
| `/cooldown amazon 90` | set (or `off`) a company's cooldown — days to wait between applications. Companies rarely publish one; JobPilot never assumes one |
| `/dream` | jobs at your dream companies (`tracker.dream_companies`, FAANG by default) and your applications there |
| `/pause`, `/resume` | stop or restart automatic recruiter outreach (JobPilot never applies to jobs for you) |

Commands that change something: `/answer`, `/cooldown`, `/pause`, `/resume`, the batch buttons, and your own answers
on the tracker buttons (applied + resume, not yet, not interested, what an employer email meant, Undo).
"Applied" from a button is recorded with confirmation method `self_reported` — your statement is the evidence.

Messages from any other chat are recorded as rejected and get **no reply**. Unknown commands get a pointer to
`/help`. Each update is recorded before it is answered and the polling offset is stored, so nothing is answered
twice. Only one process polls at a time (a file lock), so a second `jobpilot serve` or `jobpilot telegram poll`
waits instead of competing. Set `JOBPILOT_TELEGRAM_COMMANDS_ENABLED=false` for notifications without commands.

## Notifications

Sent by the `notify` scheduler task (or **Telegram → Check and notify now**, or `jobpilot telegram notify`).
Telegram is for alerts; the dashboard is where you work. **On by default:** high matches, application status
changes, replies, follow-ups due, employer emails, sent / bounced / Gmail-draft outreach, failures and the daily
summary. **Off by default** (turn on in Settings → notifications): the new-relevant-jobs digest, draft reviews, form
questions, recruiter tasks and "Did you apply?" (the **My tracker** page asks the same).

- 🎯 a new high match (score ≥ `high_match_min_score`, default: matching `strong_threshold`) — one message each
- 🔎 new relevant jobs (score ≥ `relevant_min_score`, default: matching `relevance_threshold`) — grouped
- ✍️ email drafts ready for your review — grouped
- 📋 application status changes — grouped
- 📬 a recruiter reply detected (Gmail sync) — one message each
- ⏰ follow-ups due — grouped (JobPilot never sends follow-ups)
- ⚠️ failed tasks (collection, Gmail sync, health check…) and job sources that failed — grouped
- 🙋 "Did you apply?" (`apply_check`) — one message per job with Apply / ✅ I applied / ⏳ Not yet / 🚫 Not
  interested, at most `tracker.ask_daily` (default 5) per local day, only for relevant, not-ineligible jobs found at
  least a day ago that you haven't answered. A new high match also carries **✅ I applied**.
- 📬 employer emails about your applications (`email_outcome`; Gmail sync, headers only — From / Subject / Date,
  never the body). For applications in applied / assessment / interview, a new inbox message whose subject is about
  an application and names the company (subject, sender name or domain):
  - unambiguous subject (rejection, assessment, interview, offer) and one matching application → the status is
    updated automatically (`tracker.auto_outcomes`) and the message has **↩️ Undo**;
  - anything else ("Your application to X", several applications at that company) → you're asked, with buttons
    Rejected / Assessment / Interview / Offer / Nothing changed. Nothing changes until you choose.
  "Thank you for applying" mail and mail not about an application (orders, accounts) are ignored.
- 📊 the daily summary, once a day at the first notify pass after `daily_summary_hour` (local time)

Preferences: **Settings → notifications** (turn kinds on/off, thresholds, summary hour, items per message, retry
attempts). A kind you turn off is recorded as seen, so it is not sent later either.

The first notify pass after setup records everything that already exists as history and sends nothing about it.
Each event is announced once (keys in `notification_keys`). A message is marked *sent* only when Telegram returns
its message id. Rate limits (`429`) and network/server errors are retried with backoff; configuration errors (bad
token, blocked bot, wrong chat id) stop the pass and show on **Telegram** and **System**. A lost response after
Telegram accepted a message can, rarely, cause it to arrive twice; it is never silently dropped.

The daily summary shows real database counts: jobs discovered today, new relevant jobs, high matches, drafts awaiting
review, applications awaiting approval, applications recorded as submitted today (only with confirmation evidence),
replies detected, follow-ups due, failed tasks, and the last successful collection. When there was nothing, it says so.

## Security

- The token is read from the environment as a secret value: settings, errors, CLI output, pages, API responses and
  logs never contain it (log files also pass through a filter that scrubs anything token-shaped).
- Messages are plain text (no HTML/Markdown), so text from job postings can't add links or formatting.
- Links in messages point to your local dashboard (`http://127.0.0.1:8765/...`) and only work on your laptop.
- Replies contain no email addresses, resume text or credentials.

## Troubleshooting

| Problem | Fix |
|---|---|
| `token is wrong or was revoked` | check `JOBPILOT_TELEGRAM_BOT_TOKEN`; regenerate with @BotFather `/token` if leaked |
| `bot can't message this chat` / `chat not found` | press Start in the bot chat; check `JOBPILOT_TELEGRAM_CHAT_ID` |
| `another program is reading this bot's updates` (409) | only one poller per bot: stop the other process, or a webhook set elsewhere |
| no answers to commands | restart `jobpilot serve` after setting the chat id; `/telegram` shows whether it is polling |
| nothing is sent | enable the `notify` task and run a scheduler; `jobpilot telegram notify --dry-run` shows what would be sent |

Nothing runs while the laptop is asleep or JobPilot is stopped. On the next start, due tasks run once and the
poller fetches messages sent in the meantime (Telegram keeps undelivered updates for about 24 hours).
