# Running JobPilot in Docker

## The easy way: `./jobpilot-docker`

```bash
./jobpilot-docker switch-to-docker   # once: stop + disable the native service (asks), back up, start Docker
./jobpilot-docker start              # build if needed, start, wait until healthy → http://127.0.0.1:8765
./jobpilot-docker stop               # clean stop (scheduler waits for a running task)
./jobpilot-docker logs -f            # follow the log;  status: container + /health
./jobpilot-docker update             # backup, rebuild from the current code, restart (migrations run on start)
./jobpilot-docker cli doctor         # any jobpilot command inside the container
./jobpilot-docker switch-to-native   # stop Docker, re-enable the native systemd service
```

- **Your data, not a copy:** the container mounts this repository's `data/` folder (database, resumes, backups,
  Gmail token) and runs as your own user, so files stay yours and switching back and forth needs no export / import.
  `JOBPILOT_DATA_SOURCE=/other/dir` uses another folder.
- **Settings:** on first start `.env.docker` (git-ignored, mode 600) is made from `.env.docker.example` plus your
  `.env` (Telegram, Gmail, scheduler); paths, host and port are never copied, `TZ` is your host's time zone and
  browser-assisted applying is switched off. Edit `.env.docker` and `./jobpilot-docker restart` to change them.
- **One JobPilot at a time:** `start` refuses while the native service is running *or enabled* (it would come back
  at login and both would collect, notify and poll Telegram). A separate test copy may run next to it when both the
  data folder and the env file differ:
  `JOBPILOT_DATA_SOURCE=/tmp/jp-data JOBPILOT_ENV_FILE_DOCKER=/tmp/jp.env JOBPILOT_HOST_PORT=18765 COMPOSE_PROJECT_NAME=jobpilot-test ./jobpilot-docker start`.
- **In the image:** LaTeX (`latexmk`, `pdflatex`, the packages the resume template uses) and `pdftotext`, so tailored
  resumes and their checks work. Not included: Chrome / Playwright (browser-assisted applications, the Apply agent's
  form submission). `docker build --build-arg WITH_LATEX=false` builds the ~200 MB image without LaTeX.

The rest of this page is the manual / server set-up the script wraps.

Docker is an **additional** deployment option, for hosting JobPilot on a server later. On your laptop, the native
install (`.venv` + the `jobpilot.service` systemd user service) remains the recommended mode. Both run the same
code; only paths and binding differ.

> **Never run native and Docker JobPilot at the same time against the same data or the same Telegram bot.**
> Two schedulers would collect and notify twice, and two pollers on one bot make Telegram reject both (HTTP 409).
> Stop one before starting the other.

## What the container is

- One container, one process: `jobpilot serve` (dashboard + internal scheduler + Telegram poller, exactly as in
  native mode). No extra scheduler, worker, Redis or Celery.
- `python:3.12.14-slim-trixie`, pinned by digest; runtime dependencies pinned in `docker/requirements.lock`
  (the versions the test suite passes with). About 200 MB, plus LaTeX (see above).
- Runs as the unprivileged user `jobpilot` (uid/gid 10001), read-only root filesystem, all capabilities dropped,
  `no-new-privileges`, `/tmp` as a 256 MB tmpfs.
- `init: true` forwards `docker stop`'s SIGTERM to JobPilot, which stops the scheduler (waiting for a running task)
  and exits; the log ends with `shutdown complete … "scheduler_stopped": true`. Exit code 143 after a stop is
  normal (uvicorn re-raises SIGTERM after shutting down cleanly). `stop_grace_period` is 90 s.
- `restart: unless-stopped`: restarted after a crash or reboot, not after you stop it.
- Health check: the app's own `/health` (database, migrations, scheduler pass not overdue, integrations).
  An unhealthy container is reported (`docker ps`), not restarted automatically.
- Included: LaTeX and `pdftotext` (tailored resumes, resume import). Not included: Google Chrome/Playwright
  (browser-assisted applications). Use the native install for those.

## Persistent data and secrets

Everything JobPilot writes lives under **`/data`** (one volume):

| Path in the container | Contents |
|---|---|
| `/data/jobpilot.db` (+ `-wal`, `-shm`) | SQLite database: jobs, applications, drafts, notification history, Telegram offset, settings |
| `/data/files/` | resumes and other uploaded/generated files |
| `/data/backups/` | backups (scheduled, pre-migration, pre-restore) |
| `/data/logs/jobpilot.log` | structured application log |
| `/data/secrets/` | Gmail OAuth client and token (mode 600) |
| `/data/locks/` | task lock files |

Default storage is the named volume `jobpilot_jobpilot-data`. To use a host directory instead (easier to back up on
a server), set `JOBPILOT_DATA_SOURCE=/srv/jobpilot/data` when running compose; that directory must be owned by the
container user (`sudo chown -R 10001:10001 /srv/jobpilot/data`, `chmod 700`), or run the container as your own user
with `JOBPILOT_UID=$(id -u) JOBPILOT_GID=$(id -g)`.

Never in the image (enforced by the allow-list `.dockerignore`; verified by scanning the built image): `.env`,
`.env.docker`, `data/`, databases, backups, logs, resumes, Gmail client secrets or tokens, Telegram tokens, `.venv`.

Settings come from **`.env.docker`** (copy of `.env.docker.example`, git-ignored, `chmod 600`), passed as environment
variables. `compose.yaml` fixes `JOBPILOT_DATA_DIR=/data`, host `0.0.0.0` inside the container, and port 8765, so
values copied from the native `.env` can't point somewhere else. Set `TZ` (e.g. `Asia/Kolkata`): the daily summary
hour and "today" use local time, and containers default to UTC. Note that environment variables (including the
Telegram token) are visible to anyone who can run `docker inspect` on the host.

## A. Native laptop mode (recommended now)

```bash
systemctl --user status jobpilot.service --no-pager       # running?
systemctl --user restart jobpilot.service                 # after code or .env changes
systemctl --user stop jobpilot.service                    # stop (scheduler and Telegram stop too)
systemctl --user start jobpilot.service
journalctl --user -u jobpilot.service -n 50 --no-pager    # service output
tail -n 50 ~/projects/jobpilot/data/logs/jobpilot.log     # application log
cd ~/projects/jobpilot && .venv/bin/jobpilot scheduler status
curl -s http://127.0.0.1:8765/health
```

## B. Build and verify with Docker (isolated, safe on the laptop)

Use a separate project name, port and env file with **Telegram and Gmail disabled**, so the test container can't
compete with the native service or touch its data:

```bash
cd ~/projects/jobpilot
docker build -t jobpilot:local .
cp .env.docker.example /tmp/jobpilot-test.env            # leaves Telegram and Gmail disabled
export COMPOSE_PROJECT_NAME=jobpilot-test JOBPILOT_ENV_FILE_DOCKER=/tmp/jobpilot-test.env JOBPILOT_HOST_PORT=18765
docker compose config --quiet && echo valid               # validate the configuration
docker compose up -d --no-build
docker compose ps                                        # wait for "healthy" (about a minute)
curl -s http://127.0.0.1:18765/health
docker compose logs --tail 30
docker compose exec jobpilot jobpilot scheduler status
docker compose stop                                      # graceful stop
docker compose down -v                                   # remove the TEST container and its volume
unset COMPOSE_PROJECT_NAME JOBPILOT_ENV_FILE_DOCKER JOBPILOT_HOST_PORT
```

`docker compose down -v` deletes the volume: only use `-v` on a test project.

## C. Hosting on a server (future)

1. **Install** Docker and Compose; copy the repository (or just `Dockerfile`, `compose.yaml`, `.dockerignore`,
   `pyproject.toml`, `README.md`, `src/`, `docker/`) and build: `docker build -t jobpilot:$(date +%Y%m%d) -t jobpilot:local .`
2. **Configure**: `cp .env.docker.example .env.docker && chmod 600 .env.docker`; fill in Telegram (a bot no other
   JobPilot polls) and `TZ`. Optionally `JOBPILOT_DATA_SOURCE=/srv/jobpilot/data` (see above).
3. **Start**: `docker compose up -d`, then `docker compose ps` (healthy) and `docker compose logs --tail 50`.
   A fresh volume gets a new database automatically. Enable tasks in **Settings → scheduler**.
4. **Gmail** (optional): `jobpilot gmail auth` needs a browser on the same machine, so it can't run in the container.
   Authorise with the native install (or any machine with this repository and the same OAuth client), then copy the
   two files in through stdin, so they never need looser permissions on the host:
   ```bash
   for f in gmail_client_secret.json gmail_token.json; do
     docker compose run --rm --no-deps -T --entrypoint sh jobpilot \
       -c "umask 077; mkdir -p /data/secrets && cat > /data/secrets/$f" < ~/projects/jobpilot/data/secrets/$f
   done
   docker compose exec jobpilot jobpilot gmail status       # then set JOBPILOT_GMAIL_ENABLED=true and restart
   ```
   Google expires refresh tokens of apps in *Testing* after about 7 days: re-authorise natively and copy again.
5. **Backups**: the `backup` task writes verified backups to `/data/backups` (kept: Settings → scheduler →
   `keep_backups`). They are inside the same volume, so also copy them off the server, e.g. daily:
   `docker compose cp jobpilot:/data/backups ./offsite-backups` (or back up the host directory).
6. **Access**: keep the published port on `127.0.0.1` (the default). JobPilot has **no login**; Docker doesn't change
   that, so it is not safe on the public internet. Reach the dashboard through an SSH tunnel:
   `ssh -L 8765:127.0.0.1:8765 you@server`, then open http://127.0.0.1:8765 on your laptop.
   A reverse proxy on a domain name is **not supported yet**: JobPilot only accepts `localhost`/`127.0.0.1` as the
   Host and as the Origin of form posts (CSRF protection), so dashboard actions would be rejected. Supporting it
   needs a configurable allowed-host setting plus authentication in front (e.g. an identity-aware proxy or VPN).
7. **Upgrade**: back up, rebuild with a new tag, restart; migrations run on start (with their own backup first):
   ```bash
   docker compose exec jobpilot jobpilot backup create --label pre-upgrade
   docker image tag jobpilot:local jobpilot:previous
   git pull && docker build -t jobpilot:local .
   docker compose up -d && docker compose ps && docker compose logs --tail 50
   ```
8. **Roll back** if the new image misbehaves:
   ```bash
   docker compose stop
   docker image tag jobpilot:previous jobpilot:local && docker compose up -d
   ```
   If the new version had already migrated the database, also restore the pre-upgrade backup (step D.3 below,
   using the backup inside the volume: `jobpilot backup restore --name <file>.zip --yes`).

## D. Moving the native install into Docker (manual; not done automatically)

1. **Stop native JobPilot** (scheduler and Telegram poller stop with it):
   `systemctl --user stop jobpilot.service && systemctl --user disable jobpilot.service`
2. **Back up and verify** natively:
   ```bash
   cd ~/projects/jobpilot
   B=$(.venv/bin/jobpilot backup create --label to-docker | tail -1); echo "$B"
   .venv/bin/python -c "from jobpilot.services.backup import verify_backup; import sys; print(verify_backup(__import__('pathlib').Path(sys.argv[1])) or 'ok')" "$B"
   ```
3. **Restore into the container's volume** (stream through stdin; the container must not be running):
   ```bash
   cp .env.docker.example .env.docker && chmod 600 .env.docker    # then copy your Telegram settings and TZ into it
   docker compose run --rm --no-deps jobpilot init
   docker compose run --rm --no-deps -T --entrypoint sh jobpilot \
     -c 'umask 077; cat > /tmp/restore.zip && jobpilot backup restore --name /tmp/restore.zip --yes' < "$B"
   ```
   The backup holds the database (including notification history and the Telegram offset) and `files/` (resumes);
   it never contains OAuth tokens: copy the Gmail files as in C.4.
4. **Start and verify** before calling it done:
   ```bash
   docker compose up -d && docker compose ps                       # healthy
   docker compose exec jobpilot jobpilot doctor
   docker compose exec jobpilot jobpilot scheduler status
   docker compose exec jobpilot jobpilot telegram status
   docker compose exec jobpilot jobpilot gmail check               # read-only
   ```
   Check the dashboard (jobs, drafts, Telegram page), and that `/status` in Telegram is answered.
5. **Roll back to native**: `docker compose stop` (the container must be stopped first), then
   `systemctl --user enable --now jobpilot.service`. Anything that changed while Docker ran is only in the volume:
   if you need it, back it up in the container (`docker compose exec jobpilot jobpilot backup create`), copy the zip
   out (`docker compose cp jobpilot:/data/backups/<file>.zip .`) and restore it natively
   (`.venv/bin/jobpilot backup restore --name ./<file>.zip`).

## Verified on 2026-10-17 (isolated test projects; live data untouched)

Image build, `docker compose config`, first start creating a database, healthy status, `/health`, one process and
no extra workers, data surviving container recreation, automatic restart after the app process was killed (database
integrity ok), clean shutdown, no secrets/databases/resumes in the image (byte scan for the real tokens), and the
migration procedure above (restore + Gmail files) with synthetic data.
