"""Safe local file storage for uploads (resumes, evidence).

- Allow-listed extensions with magic-byte checks.
- Size limit from settings.
- Content-addressed names; user filenames are never used as paths.
- Reads resolve strictly inside ``files_dir``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from jobpilot.config import Settings

ALLOWED = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
    ".txt": ("text/plain", ()),
    ".md": ("text/markdown", ()),
    ".html": ("text/html", ()),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".eml": ("message/rfc822", ()),
}
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class StorageError(ValueError):
    pass


@dataclass
class StoredFile:
    rel_path: str
    sha256: str
    size: int
    mime_type: str
    original_name: str


def safe_name(name: str) -> str:
    base = Path(name or "file").name
    stem = _SAFE.sub("_", Path(base).stem)[:60].strip("._") or "file"
    return stem + Path(base).suffix.lower()


def store(settings: Settings, subdir: str, filename: str, data: bytes,
          allowed: tuple[str, ...] | None = None) -> StoredFile:
    if not re.fullmatch(r"[a-z_]+", subdir):
        raise StorageError("invalid storage area")
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED or (allowed and ext not in allowed):
        raise StorageError(f"File type {ext or '(none)'} not allowed"
                           + (f"; allowed: {', '.join(allowed)}" if allowed else ""))
    if not data:
        raise StorageError("File is empty")
    if len(data) > settings.max_upload_bytes:
        raise StorageError(f"File too large (max {settings.max_upload_bytes // 1_000_000} MB)")
    mime, magics = ALLOWED[ext]
    if magics and not any(data.startswith(m) for m in magics):
        raise StorageError(f"File content does not look like {ext}")
    if not magics:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageError("Text file is not valid UTF-8") from exc
    digest = hashlib.sha256(data).hexdigest()
    target_dir = settings.files_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{digest[:16]}_{safe_name(filename)}"
    path = target_dir / name
    if not path.exists():
        path.write_bytes(data)
        path.chmod(0o600)
    return StoredFile(rel_path=f"{subdir}/{name}", sha256=digest, size=len(data), mime_type=mime,
                      original_name=Path(filename).name[:300])


def resolve(settings: Settings, rel_path: str | None) -> Path:
    if not rel_path:
        raise StorageError("No file")
    root = settings.files_dir.resolve()
    path = (root / rel_path).resolve()
    if root not in path.parents or not path.is_file():
        raise StorageError("File not found")
    return path


def delete(settings: Settings, rel_path: str | None) -> None:
    try:
        resolve(settings, rel_path).unlink()
    except StorageError:
        pass
