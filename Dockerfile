# syntax=docker/dockerfile:1
# JobPilot container image: the same application as the native install, for future hosting.
# No secrets, databases, resumes or .env files are ever copied in (see .dockerignore); all state lives in /data.

ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# ---- build: install pinned runtime dependencies and JobPilot into a virtualenv ----------------------------
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY docker/requirements.lock /tmp/requirements.lock
# --no-deps + pip check: exactly the pinned set, and the build fails if the lock is incomplete.
RUN pip install --no-deps -r /tmp/requirements.lock
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-deps /src && pip check && python -m compileall -q /opt/venv/lib

# ---- runtime ------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ARG JOBPILOT_UID=10001
ARG JOBPILOT_GID=10001
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOBPILOT_DATA_DIR=/data \
    JOBPILOT_HOST=0.0.0.0 \
    JOBPILOT_PORT=8765 \
    JOBPILOT_ALLOW_REMOTE_BIND=true
RUN groupadd --system --gid "${JOBPILOT_GID}" jobpilot \
    && useradd --system --uid "${JOBPILOT_UID}" --gid jobpilot --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin jobpilot \
    && mkdir -p /data /app \
    && chown jobpilot:jobpilot /data && chmod 700 /data
COPY --from=build /opt/venv /opt/venv
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/jobpilot-entrypoint
WORKDIR /app
USER jobpilot
VOLUME ["/data"]
EXPOSE 8765
STOPSIGNAL SIGTERM
# The application's own health endpoint (database, scheduler, integrations); 503 = unhealthy.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=8).status == 200 else 1)"]
ENTRYPOINT ["jobpilot-entrypoint"]
CMD ["serve"]
