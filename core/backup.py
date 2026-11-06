"""Backup zip for Admin (Phase 6) and the checks shared with the restore command.

Zip layout:
    backup_info.txt      date, environment, app version, kind, who made it
    database.sqlite3     a safe copy of the database (SQLite's own backup, app keeps running)
    media/...            only in “Database + media”: everything under MEDIA_ROOT
"""

import logging
import shutil
import sqlite3
import tempfile
import tomllib
import zipfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from core.models import BackupLog

logger = logging.getLogger(__name__)

DB_NAME = "database.sqlite3"
INFO_NAME = "backup_info.txt"
MEDIA_PREFIX = "media/"
REMINDER_DAYS = 7
CHUNK = 1024 * 1024


def app_version() -> str:
    try:
        with open(settings.BASE_DIR / "pyproject.toml", "rb") as file:
            return tomllib.load(file)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        logger.warning("Could not read the app version from pyproject.toml")
        return "unknown"


def media_files():
    """Every file under MEDIA_ROOT, as (path on disk, name inside the zip)."""
    root = Path(settings.MEDIA_ROOT)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, MEDIA_PREFIX + path.relative_to(root).as_posix()


def database_size() -> int:
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA page_count")
        pages = cursor.fetchone()[0]
        cursor.execute("PRAGMA page_size")
        return pages * cursor.fetchone()[0]


def size_estimates() -> dict:
    """Rough sizes before zipping, shown next to the buttons (media is stored, not shrunk)."""
    database = database_size()
    media = sum(path.stat().st_size for path, _name in media_files())
    return {"database": database, "full": database + media, "media": media}


def copy_database(target: Path) -> None:
    """Copy the live database with SQLite's backup, which is safe while others write."""
    connection.ensure_connection()
    copy = sqlite3.connect(target)
    try:
        connection.connection.backup(copy)
    finally:
        copy.close()


def _info_text(kind: str, user, created_at) -> str:
    return "\n".join(
        [
            "Stock Audit backup",
            f"created_at: {timezone.localtime(created_at):%Y-%m-%d %H:%M:%S %Z}",
            f"environment: {settings.DJANGO_ENV}",
            f"app_version: {app_version()}",
            f"kind: {kind}",
            f"made_by: {user.username}",
            "",
            "Restore on a test server with:",
            "  uv run python manage.py restore_backup <this zip>",
            "",
        ]
    )


def build_zip(kind: str, user) -> tuple[Path, Path]:
    """Make the zip in a new temp folder. Returns (zip path, temp folder to delete after)."""
    if kind not in BackupLog.Kind.values:
        raise ValueError(f"Unknown backup kind: {kind}")
    now = timezone.now()
    folder = Path(tempfile.mkdtemp(prefix="stock-audit-backup-"))
    try:
        db_copy = folder / DB_NAME
        copy_database(db_copy)
        zip_path = (
            folder
            / f"stock-audit-{settings.DJANGO_ENV}-{kind}-{timezone.localtime(now):%Y%m%d-%H%M}.zip"
        )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(INFO_NAME, _info_text(kind, user, now))
            archive.write(db_copy, DB_NAME)
            if kind == BackupLog.Kind.FULL:
                for path, name in media_files():
                    # Photos and videos are already compressed; storing them is much faster.
                    archive.write(path, name, compress_type=zipfile.ZIP_STORED)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return zip_path, folder


def stream_and_clean(zip_path: Path, folder: Path):
    """Send the zip in chunks, then delete the temp folder (also if the download stops)."""
    try:
        with open(zip_path, "rb") as file:
            while chunk := file.read(CHUNK):
                yield chunk
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def last_backup() -> BackupLog | None:
    return BackupLog.objects.select_related("created_by").first()


def reminder(now=None) -> dict | None:
    """The banner text data when no backup was made in REMINDER_DAYS days, else None."""
    now = now or timezone.now()
    last = BackupLog.objects.only("created_at").first()
    if last is None:
        return {"days": None}
    if now - last.created_at < timedelta(days=REMINDER_DAYS):
        return None
    return {"days": (now - last.created_at).days}


# ---------- Checks used by the restore command ----------


class BackupError(Exception):
    """The zip can't be restored; the message says why in plain words."""


def check_zip(path: Path) -> zipfile.ZipFile:
    """Open and check a backup zip: right files, safe names, and a healthy database."""
    if not path.is_file():
        raise BackupError(f"File not found: {path}")
    if not zipfile.is_zipfile(path):
        raise BackupError(f"{path.name} is not a zip file.")
    archive = zipfile.ZipFile(path)
    names = set(archive.namelist())
    if INFO_NAME not in names or DB_NAME not in names:
        archive.close()
        raise BackupError(
            f"{path.name} is not a Stock Audit backup (missing {INFO_NAME} or {DB_NAME})."
        )
    for name in names - {INFO_NAME, DB_NAME}:
        parts = Path(name).parts
        if not name.startswith(MEDIA_PREFIX) or ".." in parts or Path(name).is_absolute():
            archive.close()
            raise BackupError(f"Unexpected file in the backup: {name}. Restore stopped.")
    return archive


def check_database(db_path: Path) -> None:
    try:
        check = sqlite3.connect(db_path)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            check.close()
    except sqlite3.DatabaseError as error:
        raise BackupError(f"The database in the backup can't be read: {error}") from error
    if result != "ok":
        raise BackupError(f"The database in the backup is damaged: {result}")
    if "django_migrations" not in tables:
        raise BackupError("The database in the backup is not a Stock Audit database.")
