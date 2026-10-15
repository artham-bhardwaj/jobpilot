"""Database engine, sessions and migration helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.config import Settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(settings: Settings) -> Engine:
    url = settings.db_url
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if settings.db_file:
            settings.db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config(settings: Settings):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", settings.db_url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision(settings: Settings) -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(settings)).get_current_head()


def migrate(settings: Settings, engine: Engine | None = None, backup_first: bool = True) -> dict:
    """Upgrade the database to the latest revision.

    If the database already holds data and migrations are pending, a backup is
    taken first so existing records can always be recovered.
    """
    from alembic import command

    engine = engine or make_engine(settings)
    cur = current_revision(engine)
    head = head_revision(settings)
    result = {"from": cur, "to": head, "backup": None}
    if cur == head:
        return result
    has_tables = bool(inspect(engine).get_table_names())
    if backup_first and has_tables and settings.db_file and settings.db_file.exists():
        from jobpilot.services.backup import create_backup

        result["backup"] = str(create_backup(settings, engine, label=f"pre-migrate-{cur or 'none'}"))
    cfg = alembic_config(settings)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    log.info("database migrated", extra={"from_rev": cur, "to_rev": head})
    return result
