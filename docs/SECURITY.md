# Security and privacy notes

## Threat model

JobPilot is a single-user app on your own computer, holding personal data (profile, resumes,
contacts, application history). The main risks: (1) other websites in your browser driving the
local app, (2) untrusted content from job pages or imports, (3) secrets or personal files leaking
through Git or logs, (4) automation that oversteps (sending, submitting, scraping).

## Controls

| Area | Control |
|---|---|
| Network exposure | Binds to `127.0.0.1` by default; any other host is refused unless `JOBPILOT_ALLOW_REMOTE_BIND=true`. There is no login, so don't expose it. |
| DNS rebinding | `TrustedHostMiddleware` only accepts `127.0.0.1`, `localhost`, `::1`. |
| CSRF | State-changing requests with a cross-site `Origin`/`Referer`/`Sec-Fetch-Site` are rejected (403). |
| XSS | Jinja2 autoescaping everywhere; no `|safe` on user/external data; untrusted URLs are kept only if they are absolute `http(s)` (blocks `javascript:`/`data:` links); strict CSP (`script-src 'self'`, no inline scripts), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin` (no referrer to external sites). |
| Template injection | Email templates render in Jinja2's `SandboxedEnvironment`. |
| Uploads | Extension allow-list plus magic-byte checks, 10 MB limit, content-addressed names (user filenames never become paths); downloads resolve strictly inside `data/files`. |
| Backups | Created 0600 in a 0700 directory; exclude OAuth tokens and the browser profile. Restore validates member paths (no `..`/absolute paths/unexpected files), limits total size (2 GB) and member count, runs `PRAGMA integrity_check`, and backs up the current state first. Backups are **not encrypted**. |
| SSRF | Only `http(s)`, no credentials in URLs. Every request **and every redirect hop** (followed manually, max 5) must resolve exclusively to globally routable addresses: loopback, private, link-local, CGNAT, reserved, multicast and IPv4-mapped-IPv6 forms are refused before any connection. Residual risk: DNS can change between the check and the connection (rebinding); acceptable for a single-user local tool, documented in IMPLEMENTATION_STATUS.md. |
| Response size | Bodies are streamed with a hard cap (5 MB pages/APIs, 512 KB robots.txt); an oversized `Content-Length` is rejected before reading. An unusable robots.txt is treated as "disallow". |
| File permissions | Process umask 0o077; `data/` and its sub-directories 0700; database (+WAL/SHM), logs, backups, exports and tokens 0600. Existing looser permissions are tightened on start-up; `/health` reports any that remain. |
| API docs | Swagger/ReDoc UIs are not served (they load third-party CDN scripts). `/openapi.json` only if `JOBPILOT_ENABLE_OPENAPI=true`. |
| Error handling | Missing records → 404 page, invalid input → 400 page, unexpected errors → generic 500 page (details only in the local log). |
| Shell | No `shell=True` anywhere; the browser helper is started with an argument list. |
| CSV export | Cells starting with `= + - @` are prefixed to prevent spreadsheet formula injection. |
| Secrets | `.env`, `data/` (DB, resumes, backups, logs, token, browser profile), `client_secret*.json` and `token*.json` are git-ignored. The OAuth token is written with mode 0600 in a 0700 directory and excluded from backups. |
| Logs | Structured JSON logs. Audit entries record *which* fields changed, not their values. |
| Outbound data | Nothing is transmitted except (a) job-page/API fetches you configure, (b) Gmail API calls after you enable and authorise Gmail, (c) pages you open in the assisted browser. |
| Automation limits | No auto-submission; no checkbox/consent/legal answers; pauses on CAPTCHA/MFA/login/security checks; never bypasses access controls; autofill only on supported hosted forms; one browser session at a time. |
| Email limits | Approved drafts only, explicit `SEND` confirmation, daily send cap (default 10), DNC re-checked at send time, no bulk send feature. |
| Scraping etiquette | robots.txt respected, per-host rate limit, honest User-Agent, bounded retries honouring `Retry-After`. |

## Your responsibilities

- Check each site's terms before adding it as a source.
- Keep `data/` private. Backups contain your database and resumes. Store copies securely.
- Don't set `JOBPILOT_ALLOW_REMOTE_BIND=true` on untrusted networks.
- If you use Gmail, keep the OAuth client and token private; `jobpilot gmail revoke` disconnects.

## Deleting data

System → *Your data*: delete profile facts, contacts, drafts, or everything (records, stored files,
browser profile, Gmail token). A safety backup is taken first unless you also choose to delete
backups. CLI: `jobpilot delete-data <scope> [--include-backups]`.
