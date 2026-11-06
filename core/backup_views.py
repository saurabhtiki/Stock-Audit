"""Admin “Backup” screen: download a zip of the database (and media), see the last backup."""

import logging
import sqlite3

from django.contrib import messages
from django.http import StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core import backup
from core.activity import log_activity
from core.models import BackupLog
from core.permissions import admin_required

logger = logging.getLogger(__name__)


@admin_required
def backup_page(request):
    try:
        sizes = backup.size_estimates()
    except OSError:
        logger.exception("Could not work out the backup sizes")
        sizes = None
    return render(
        request,
        "core/backup.html",
        {
            "last": backup.last_backup(),
            "recent": BackupLog.objects.select_related("created_by")[:10],
            "sizes": sizes,
            "reminder_days": backup.REMINDER_DAYS,
        },
    )


@admin_required
@require_POST
def backup_download(request):
    kind = request.POST.get("kind", "")
    if kind not in BackupLog.Kind.values:
        messages.error(request, "Pick “Database only” or “Database + media”.")
        return redirect("core:backup")
    try:
        zip_path, folder = backup.build_zip(kind, request.user)
    except (OSError, sqlite3.Error):
        logger.exception("Backup (%s) by %s failed", kind, request.user.username)
        messages.error(
            request,
            "The backup could not be made (disk full or a file is locked?). "
            "Nothing was downloaded. Please try again, or ask IT to check the server disk.",
        )
        return redirect("core:backup")

    size = zip_path.stat().st_size
    record = BackupLog.objects.create(
        created_by=request.user, kind=kind, size=size, file_name=zip_path.name
    )
    log_activity(
        request.user, "backup downloaded", record, {"kind": record.get_kind_display(), "size": size}
    )
    response = StreamingHttpResponse(
        backup.stream_and_clean(zip_path, folder), content_type="application/zip"
    )
    response["Content-Length"] = str(size)
    response["Content-Disposition"] = f'attachment; filename="{zip_path.name}"'
    return response
