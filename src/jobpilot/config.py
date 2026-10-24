"""Environment-based configuration.

All settings are read from environment variables prefixed with ``JOBPILOT_``
(or a local ``.env`` file). Integrations default to disabled.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:  # e.g. files owned by another user; reported by the health check
        pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBPILOT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = Path("./data")
    database_url: str = ""
    log_level: str = "INFO"

    host: str = "127.0.0.1"
    port: int = 8765
    allow_remote_bind: bool = False

    http_timeout_seconds: float = 20.0
    http_max_retries: int = Field(default=2, ge=0, le=5)
    http_min_interval_seconds: float = Field(default=2.0, ge=0.0)
    respect_robots_txt: bool = True
    # Refuse to fetch job URLs that resolve to localhost/private networks (SSRF guard).
    block_private_addresses: bool = True
    user_agent: str = "JobPilot/0.1 (personal job-search assistant)"

    scheduler_enabled: bool = False
    scheduler_poll_seconds: int = Field(default=300, ge=30)

    browser_enabled: bool = False
    browser_channel: str = "chrome"
    browser_headless: bool = False

    gmail_enabled: bool = False
    gmail_client_secrets_path: Path | None = None
    gmail_allow_read_bodies: bool = False

    # Telegram (optional): outbound notifications and read-only commands from ONE chat.
    # The token is a SecretStr: printing settings shows "**********", never the token.
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    # Answer read-only commands (/status, /jobs...) by long polling while `jobpilot serve` runs.
    telegram_commands_enabled: bool = True

    # Apollo (optional): read from JOBPILOT_APOLLO_API_KEY only; a SecretStr, never printed. Unused unless the
    # `apollo` setting is enabled too.
    apollo_api_key: SecretStr | None = None

    max_upload_bytes: int = 10 * 1024 * 1024
    # Serve the machine-readable API schema at /openapi.json. No Swagger UI is ever served
    # (it would load scripts from a third-party CDN).
    enable_openapi: bool = False

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def _chat_id(cls, v):
        if v in (None, ""):
            return None
        v = str(v).strip()
        if not v.lstrip("-").isdigit():
            raise ValueError("JOBPILOT_TELEGRAM_CHAT_ID must be a numeric Telegram chat id")
        return v

    @field_validator("telegram_bot_token", "apollo_api_key", mode="before")
    @classmethod
    def _token(cls, v):
        return None if v in (None, "") else v

    @field_validator("log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        return v

    # Derived paths -------------------------------------------------------
    @property
    def data_path(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_path / 'jobpilot.db'}"

    @property
    def db_file(self) -> Path | None:
        url = self.db_url
        if url.startswith("sqlite:///"):
            return Path(url.removeprefix("sqlite:///"))
        return None

    @property
    def log_dir(self) -> Path:
        return self.data_path / "logs"

    @property
    def files_dir(self) -> Path:
        """Uploaded/generated files (resumes, evidence)."""
        return self.data_path / "files"

    @property
    def backup_dir(self) -> Path:
        return self.data_path / "backups"

    @property
    def secrets_dir(self) -> Path:
        return self.data_path / "secrets"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_path / "browser_profile"

    @property
    def gmail_secrets_file(self) -> Path:
        return (self.gmail_client_secrets_path or self.secrets_dir / "gmail_client_secret.json").expanduser()

    @property
    def gmail_token_file(self) -> Path:
        return self.secrets_dir / "gmail_token.json"

    def ensure_dirs(self) -> None:
        """Create the data directories privately (0700) and tighten existing sensitive files.

        Also sets a private process umask (0o077) so every file JobPilot creates later
        (database, WAL, logs, backups, uploads, browser profile) is owner-only.
        """
        os.umask(0o077)
        for d in (self.data_path, self.log_dir, self.files_dir, self.backup_dir, self.secrets_dir):
            d.mkdir(parents=True, exist_ok=True)
            _chmod(d, 0o700)
        self.secure_files()

    def secure_files(self) -> None:
        """Owner-only permissions for the database, its journals, logs, backups and secrets."""
        candidates: list[Path] = []
        if self.db_file:
            candidates += [self.db_file, Path(f"{self.db_file}-wal"), Path(f"{self.db_file}-shm"),
                           Path(f"{self.db_file}-journal")]
        for folder, pattern in ((self.log_dir, "*.log*"), (self.backup_dir, "*.zip"), (self.secrets_dir, "*")):
            if folder.exists():
                candidates += list(folder.glob(pattern))
        for path in candidates:
            if path.is_file():
                _chmod(path, 0o600)
        for d in (self.data_path / "locks", self.browser_profile_dir):
            if d.is_dir():
                _chmod(d, 0o700)

    def check_bind(self) -> None:
        if self.host not in LOCAL_HOSTS and not self.allow_remote_bind:
            raise RuntimeError(
                f"Refusing to bind to {self.host!r}: JobPilot has no login and is meant for "
                "localhost. Set JOBPILOT_ALLOW_REMOTE_BIND=true to override."
            )


class ConfigError(RuntimeError):
    pass


def env_file_path() -> Path | None:
    """The .env file to load: ``$JOBPILOT_ENV_FILE`` if set (must exist), else ``./.env`` if present.

    Relative paths inside it (e.g. ``JOBPILOT_DATA_DIR=./data``) resolve against the
    current working directory; ``jobpilot init``/``serve`` print the resolved locations.
    """
    explicit = os.environ.get("JOBPILOT_ENV_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"JOBPILOT_ENV_FILE points to a missing file: {path}")
        return path
    local = Path(".env")
    return local if local.is_file() else None


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=env_file_path())
