#!/bin/sh
# JobPilot container entrypoint. Runs exactly one `jobpilot <command>` process (default: serve), which owns the
# scheduler thread and the Telegram poller. Nothing else is started here.
set -eu

data_dir="${JOBPILOT_DATA_DIR:-/data}"
if [ ! -w "$data_dir" ]; then
    echo "JobPilot: $data_dir is not writable by uid $(id -u). Fix the volume ownership (see docs/DOCKER.md)." >&2
    exit 78
fi

# A fresh volume has no database yet: create it once (`serve` refuses to create one implicitly).
if [ "${1:-serve}" = "serve" ] && [ -z "${JOBPILOT_DATABASE_URL:-}" ] && [ ! -f "$data_dir/jobpilot.db" ]; then
    echo "JobPilot: no database in $data_dir; creating a new one (jobpilot init)"
    jobpilot init
fi

# exec: jobpilot becomes the process that receives SIGTERM from `docker stop` (via the init process).
exec jobpilot "$@"
