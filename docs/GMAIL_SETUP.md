# Optional: Gmail integration setup

JobPilot is fully usable without Gmail. Connecting it lets JobPilot create Gmail drafts, send
**approved** drafts after an explicit confirmation, detect replies (to stop follow-ups), and
optionally suggest interviews from invitation emails.

Status: implemented and tested against a mocked Google API. **It has not been exercised against a
real Google account in this build**, because that needs your own OAuth client. Please report any
issue during first use.

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
   Leave the app in **Testing**.
4. **Data access / Scopes**: add `.../auth/gmail.compose` and `.../auth/gmail.metadata` (or
   `.../auth/gmail.readonly` if you will enable body access).
5. **Clients → Create client → Application type: Desktop app**. Download the JSON.
6. Save it as `data/secrets/gmail_client_secret.json` (or set
   `JOBPILOT_GMAIL_CLIENT_SECRETS_PATH`). It's git-ignored.
7. In `.env`: `JOBPILOT_GMAIL_ENABLED=true` (and optionally `JOBPILOT_GMAIL_ALLOW_READ_BODIES=true`).
8. Run `.venv/bin/jobpilot gmail auth`. A browser opens; sign in and approve. Google will show an
   "unverified app" warning because this is your own unpublished app. Continue only if the client
   is yours. The token is saved to `data/secrets/gmail_token.json` (mode 600).
9. Check: `jobpilot gmail status`, or the **Gmail** link on the Settings page.

Notes:

- Google expires refresh tokens for apps in *Testing* status after about 7 days. Re-run
  `jobpilot gmail auth` when JobPilot reports the token expired.
- Changing `JOBPILOT_GMAIL_ALLOW_READ_BODIES` requires re-running `jobpilot gmail auth`.
- Disconnect anytime: `jobpilot gmail revoke` (revokes and deletes the token).
