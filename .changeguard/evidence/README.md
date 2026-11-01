# ChangeGuard evidence

Markdown files here are used as historical evidence when ChangeGuard analyzes changes.
Matching past incidents raise the risk score and are listed in reports.

- `incidents/` or `postmortems/`: what broke, where, and why
- `decisions/` (ADRs): why the architecture is the way it is
- `runbooks/`: how to operate and recover services

Run `changeguard evidence sync` after adding files (CI does this automatically).
