# JobPilot — instructions for Claude

## Rule: log every implementation in implementations.md

After any change to code, configuration, migrations, deployment files or documentation in this repository, add an
entry to `implementations.md` (repository root) **before reporting the work as done**:

- Heading: the local timestamp, `YYYY-MM-DD HH:MM TZ` (from `date '+%Y-%m-%d %H:%M %Z'`), then a short title.
- What was implemented or changed, and why (one or two lines).
- Files changed.
- Migrations added or applied, if any.
- Verification: tests run with exact results, and live checks (say plainly what was only mocked).
- Anything left incomplete or requiring manual action.

Newest entries go at the top. Append; never rewrite or delete earlier entries. Never put secrets, tokens or personal
data in it. Read-only investigations with no changes don't need an entry.
