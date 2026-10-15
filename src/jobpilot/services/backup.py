"""Local backup and restore of the SQLite database and stored files.

A backup is a zip containing ``jobpilot.db`` (taken with SQLite's online
backup API, so it is consistent even while the app runs), the ``files/``
directory (resumes, evidence) and ``manifest.json``. OAuth tokens and the
browser profile are deliberately excluded.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import Engine

from jobpilot import __version__
from jobpilot.config import Settings
from jobpilot.models.base import utcnow

log = logging.getLogger(__name__)

_LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_RESTORE_BYTES = 2 * 1024**3  # total uncompressed size accepted when restoring
MAX_RESTORE_MEMBERS = 50_000


class BackupError(RuntimeError):
    pass


class BackupNotFound(BackupError):
    pass


def _sqlite_copy(src: Path, dest: Path) -> None:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


def create_backup(settings: Settings, engine: Engine | None = None, label: str = "manual") -> Path:
    db_file = settings.db_file
    if not db_file or not db_file.exists():
        raise BackupError("No SQLite database file to back up")
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    label = _LABEL_RE.sub("-", label)[:40] or "manual"
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
    dest = settings.backup_dir / f"jobpilot-{stamp}-{label}.zip"
    revision = None
    if engine is not None:
        from jobpilot.db import current_revision

        revision = current_revision(engine)
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "jobpilot.db"
        _sqlite_copy(db_file, snap)
        manifest = {
            "app_version": __version__,
            "created_at": utcnow().isoformat(),
            "label": label,
            "alembic_revision": revision,
        }
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, "jobpilot.db")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            if settings.files_dir.exists():
                for p in sorted(settings.files_dir.rglob("*")):
                    if p.is_file():
                        zf.write(p, f"files/{p.relative_to(settings.files_dir).as_posix()}")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    log.info("backup created", extra={"path": str(dest), "label": label})
    return dest


def list_backups(settings: Settings) -> list[dict]:
    if not settings.backup_dir.exists():
        return []
    out = []
    for p in sorted(settings.backup_dir.glob("jobpilot-*.zip"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime)})
    return out


def resolve_backup(settings: Settings, name: str) -> Path:
    """Map a user-supplied backup name to a file inside the backup directory."""
    if not re.fullmatch(r"jobpilot-[A-Za-z0-9_.-]+\.zip", name):
        raise BackupError("Invalid backup name")
    path = (settings.backup_dir / name).resolve()
    if path.parent != settings.backup_dir.resolve():
        raise BackupError("Invalid backup name")
    if not path.is_file():
        raise BackupNotFound(f"Backup {name} not found")
    return path


def _validate_members(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > MAX_RESTORE_MEMBERS or sum(i.file_size for i in infos) > MAX_RESTORE_BYTES:
        raise BackupError("Backup archive is too large to restore safely")
    names = zf.namelist()
    if "jobpilot.db" not in names or "manifest.json" not in names:
        raise BackupError("Not a JobPilot backup (missing jobpilot.db or manifest.json)")
    for name in names:
        pp = PurePosixPath(name)
        if pp.is_absolute() or ".." in pp.parts or "\\" in name:
            raise BackupError(f"Unsafe path in backup: {name}")
        if name not in ("jobpilot.db", "manifest.json") and not name.startswith("files/"):
            raise BackupError(f"Unexpected entry in backup: {name}")


def restore_backup(settings: Settings, backup_path: Path, engine: Engine | None = None) -> dict:
    """Restore a backup. The current state is backed up first.

    The caller must dispose of any open engine afterwards (connections may
    still reference the replaced file). Migrations are applied if the backup
    predates the current schema.
    """
    db_file = settings.db_file
    if not db_file:
        raise BackupError("Restore supports SQLite databases only")
    with zipfile.ZipFile(backup_path) as zf:
        _validate_members(zf)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            zf.extractall(tmpdir)
            snap = tmpdir / "jobpilot.db"
            conn = sqlite3.connect(snap)
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
            if ok != "ok":
                raise BackupError(f"Backup database failed integrity check: {ok}")
            manifest = json.loads((tmpdir / "manifest.json").read_text())

            pre = None
            if db_file.exists():
                pre = create_backup(settings, engine, label="pre-restore")
            if engine is not None:
                engine.dispose()
            for suffix in ("-wal", "-shm"):
                Path(str(db_file) + suffix).unlink(missing_ok=True)
            db_file.parent.mkdir(parents=True, exist_ok=True)
            # Copy via the backup API so the target is a clean, consistent file.
            db_file.unlink(missing_ok=True)
            _sqlite_copy(snap, db_file)

            restored_files = tmpdir / "files"
            if settings.files_dir.exists():
                shutil.rmtree(settings.files_dir)
            if restored_files.exists():
                shutil.copytree(restored_files, settings.files_dir)
            else:
                settings.files_dir.mkdir(parents=True, exist_ok=True)

    from jobpilot.db import make_engine, migrate

    fresh = make_engine(settings)
    try:
        mig = migrate(settings, fresh, backup_first=False)
    finally:
        fresh.dispose()
    log.info("backup restored", extra={"path": str(backup_path)})
    return {"restored": backup_path.name, "pre_restore_backup": str(pre) if pre else None,
            "manifest": manifest, "migration": mig}


def prune_backups(settings: Settings, keep: int = 20) -> int:
    files = sorted(settings.backup_dir.glob("jobpilot-*.zip"), reverse=True)
    removed = 0
    for p in files[keep:]:
        p.unlink(missing_ok=True)
        removed += 1
    return removed
