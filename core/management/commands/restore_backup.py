"""Restore a backup zip made on the Backup page.

    uv run python manage.py restore_backup stock-audit-production-full-20261106-1610.zip

Replaces the database of the current environment (DJANGO_ENV) and, if the zip has media,
the media folder. The old ones are kept next to them with “before-restore” in the name.
Refuses to run on production unless --force is given.
"""

import shutil
import sqlite3
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from core import backup


class Command(BaseCommand):
    help = "Restore a Stock Audit backup zip into this environment (development / test)."

    def add_arguments(self, parser):
        parser.add_argument("zip_path", help="The backup zip downloaded from the Backup page.")
        parser.add_argument(
            "--force", action="store_true", help="Needed to restore on production. Use with care."
        )
        parser.add_argument(
            "--no-input", action="store_true", help="Don't ask “Are you sure?” (for scripts)."
        )
        parser.add_argument(
            "--old-copy-dir",
            help="Where to keep the copy of the current database (default: the data folder).",
        )

    def handle(self, *args, **options):
        env = settings.DJANGO_ENV
        if env == "production" and not options["force"]:
            raise CommandError(
                "This is the production (live) environment. Restoring would replace live data. "
                "Run it on a test server, or add --force if you are really sure."
            )
        zip_path = Path(options["zip_path"])
        try:
            archive = backup.check_zip(zip_path)
        except backup.BackupError as error:
            raise CommandError(str(error)) from error

        with archive, tempfile.TemporaryDirectory(prefix="stock-audit-restore-") as work:
            db_file = Path(archive.extract(backup.DB_NAME, work))
            try:
                backup.check_database(db_file)
            except backup.BackupError as error:
                raise CommandError(str(error)) from error
            media_names = [n for n in archive.namelist() if n.startswith(backup.MEDIA_PREFIX)]
            self.stdout.write(archive.read(backup.INFO_NAME).decode("utf-8", "replace"))

            if not options["no_input"]:
                answer = input(
                    f"This replaces the {env} database"
                    f"{' and media folder' if media_names else ''}. Type “yes” to go on: "
                )
                if answer.strip().lower() != "yes":
                    raise CommandError("Restore stopped. Nothing was changed.")

            stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
            old_dir = Path(options["old_copy_dir"] or settings.BASE_DIR / "data")
            old_dir.mkdir(parents=True, exist_ok=True)
            old_copy = old_dir / f"{env}.before-restore-{stamp}.db"
            backup.copy_database(old_copy)
            self.stdout.write(f"Old database kept as {old_copy}")

            self._restore_database(db_file)
            if media_names:
                self._restore_media(archive, media_names, stamp)
            else:
                self.stdout.write("The zip has no media; the media folder was left as it is.")

        self.stdout.write(
            self.style.SUCCESS(
                "Restore finished. Run “uv run python manage.py migrate” if the app is newer "
                "than the backup, then log in and check the data."
            )
        )

    def _restore_database(self, db_file: Path) -> None:
        """Copy page by page into the live database file (SQLite's backup, safe with WAL)."""
        connection.ensure_connection()
        source = sqlite3.connect(db_file)
        try:
            source.backup(connection.connection)
        except sqlite3.Error as error:
            raise CommandError(
                f"The database could not be replaced ({error}). Stop the app and try again."
            ) from error
        finally:
            source.close()
        connection.close()  # next query opens a fresh connection on the restored data
        self.stdout.write("Database restored.")

    def _restore_media(self, archive, names: list[str], stamp: str) -> None:
        media_root = Path(settings.MEDIA_ROOT)
        if media_root.exists():
            old_media = media_root.with_name(f"{media_root.name}.before-restore-{stamp}")
            shutil.move(media_root, old_media)
            self.stdout.write(f"Old media folder kept as {old_media}")
        media_root.mkdir(parents=True)
        for name in names:
            if name.endswith("/"):
                continue
            try:
                target = backup.media_target(media_root, name)
            except backup.BackupError as error:
                raise CommandError(str(error)) from error
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
        self.stdout.write(f"Media restored: {len(names)} files.")
