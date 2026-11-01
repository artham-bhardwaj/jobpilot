# Optional: Gmail integration setup

JobPilot is fully usable without Gmail. Connecting it lets JobPilot create Gmail drafts, send
**approved** drafts after an explicit confirmation, detect replies (to stop follow-ups), and
optionally suggest interviews from invitation emails.

Status: tested against a mocked Google API in the test suite. See "Verifying against your real account" below
for the read-only and draft-only checks to run against your own account.

## What it can access

| Scope | Why | When |
|---|---|---|
| `gmail.compose` | create drafts; send drafts you approved | always (when enabled) |
| `gmail.metadata` | read headers of threads you sent, to detect replies | default |
| `gmail.readonly` | read message bodies to find interview invitations | only if `JOBPILOT_GMAIL_ALLOW_READ_BODIES=true` (replaces `metadata`) |

There is no bulk sending, and sending is capped per day (Settings → outreach → `daily_send_limit`).

## One-time setup (≈10 minutes, free)

1. Open <https://console.cloud.google.com/> and create a project (e.g. "JobPilot personal").
2. **APIs & Services → Library**: enable **Gmail API**.
3. **OAuth consent screen** (Google Auth Platform): user type **External**, any app name, your
   email as support/developer contact. Under **Audience → Test users**, add your Gmail address.
   Then, under **Audience**, click **Publish app** (status **In production**). Apps left in **Testing**
   lose their sign-in every 7 days. A personal app does not need Google's verification: you will just see
   the "unverified app" warning when you sign in.
4. **Data access / Scopes**: add `.../auth/gmail.compose` and `.../auth/gmail.metadata` (or
   `.../auth/gmail.readonly` if you will enable body access).
5. **Clients → Create client → Application type: Desktop app**. Download the JSON.
6. Save it as `data/secrets/gmail_client_secret.json` (or set
   `JOBPILOT_GMAIL_CLIENT_SECRETS_PATH`). It's git-ignored.
7. In `.env` (in the directory you run JobPilot from): `JOBPILOT_GMAIL_ENABLED=true` (and optionally
   `JOBPILOT_GMAIL_ALLOW_READ_BODIES=true`). Put it in `.env` rather than exporting it in one terminal: the
   dashboard only sees the setting if **the `jobpilot serve` process** has it. Restart the server after changing it.
8. Run `.venv/bin/jobpilot gmail auth`. A browser opens; sign in and approve. Google will show an
   "unverified app" warning because this is your own unpublished app. Continue only if the client
   is yours. The token is saved to `data/secrets/gmail_token.json` (mode 600).
9. Check: `jobpilot gmail status`, or the **Gmail** link on the Settings page.

Notes:

- Google expires refresh tokens for apps in *Testing* status after about 7 days. If sending and reply checks stop
  with "invalid_grant" / "sign-in expired" (shown in `jobpilot gmail status`, Settings → Gmail and System health),
  publish the app (step 3) and run `jobpilot gmail auth` again. Signing in again clears the error.
- Changing `JOBPILOT_GMAIL_ALLOW_READ_BODIES` requires re-running `jobpilot gmail auth`.
- Disconnect anytime: `jobpilot gmail revoke` (revokes and deletes the token).

## How drafts reach Gmail (draft-first workflow)

A JobPilot draft and a Gmail draft are different things, and each step is explicit:

1. **Create / edit / save**: local only. Gmail is not contacted.
2. **Approve**: local only. It marks the text as yours to use; it does not create a Gmail draft or send anything.
3. **Create Gmail draft** (draft page → Gmail section, or `jobpilot gmail create-draft --draft ID`): builds the email
   (To, Subject, body, and the draft's own resume version as a PDF attachment) and asks Gmail to store it in Drafts.
   JobPilot shows success only after Gmail returns the draft id, and records that id, the thread id, and exactly
   which file was attached (name, size, SHA-256). Running it again reuses the existing Gmail draft instead of
   creating a duplicate. If a request gets no answer, JobPilot records "unknown" and checks Gmail Drafts before ever
   creating another.
4. **Send**: a separate confirmation screen shows the recipient, subject, text and attachment. Sending needs a
   ticked confirmation, respects the daily limit and do-not-contact rules, and is refused for placeholder domains
   (`.example`, `.invalid`, `.test`, `.localhost`). A message counts as sent via Gmail only when Gmail returns its
   message id. If the answer is lost, the state becomes "send outcome unknown": JobPilot never retries
   automatically, and **Check Gmail** looks at the thread to find out.
5. **I sent it myself — record manually**: for email you sent outside JobPilot. It is labelled as a manual record
   and is never shown as a Gmail send.

If the body says a resume is attached, a Gmail draft is only created when the resume file is really attached.

## Replies

`jobpilot gmail sync` (or **Gmail → Check for replies now**) looks only at messages sent through Gmail (they have a
Gmail thread id). It also notices drafts you sent from Gmail itself. A reply is a message in that thread that
isn't yours and arrived after your message; it stops follow-ups on the thread. `checked: 0` means there was nothing
to check, and the message says why (for example "1 approved but not in Gmail"). A failed Gmail request is reported
as an error, not as zero. Reply detection needs only `gmail.metadata` (headers); message bodies are never read for it.

## Verifying against your real account

Nothing below sends email.

```bash
cd ~/projects/jobpilot
.venv/bin/jobpilot gmail status                     # ready_ready must be true; ready_problems lists anything missing
.venv/bin/jobpilot gmail check                      # read-only call to Gmail: prints the authorised address
.venv/bin/jobpilot gmail test-draft --to jobpilot-test@example.com --resume-version 1
                                                    # creates a labelled test draft in Gmail Drafts; prints its draft id
.venv/bin/jobpilot gmail inspect --draft <ID>       # reads that draft back FROM Gmail: To, Subject, labels (DRAFT,
                                                    # not SENT), attachment name and size
.venv/bin/jobpilot gmail draft-status --draft <ID> --refresh   # local record vs what Gmail reports
```

Then open Gmail → Drafts: the "[JobPilot test] … do not send" draft is there with the PDF attached, and Sent has
nothing new. Delete the test draft in Gmail, and cancel the local test draft on its page in JobPilot.

## Changing scopes

If `JOBPILOT_GMAIL_ALLOW_READ_BODIES` changes, or Gmail reports insufficient permissions, run
`jobpilot gmail auth` again and grant every requested permission. JobPilot never widens permissions on its own.
