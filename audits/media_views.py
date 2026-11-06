"""Screens for attachments: the media panel (list + upload), remove, photo-in-report tick,
and the only way to open a stored file (Branch right checked, never a public /media/ URL)."""

import logging
import re

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from audits import media
from audits.models import Action, Attachment, AuditLine, Query
from audits.permissions import (
    can_add_media,
    can_remove_media,
    can_tick_report,
    get_visible_audit,
)

logger = logging.getLogger(__name__)

TARGETS = ("audit", "line", "query", "action")
NO_MEDIA_RIGHT = (
    "You need the “Media” right in this Branch (for a query or action, the “Reply” or "
    "“Actions” right also works). Media can't change once the audit is Locked "
    "or the query is Closed."
)


def _resolve(request, target: str, pk: int) -> dict:
    """The audit / line / query / action the panel belongs to, if the user may see it."""
    if target not in TARGETS:
        raise Http404("Unknown media target.")
    line = query = action = None
    if target == "audit":
        audit = get_visible_audit(request, pk)
    elif target == "line":
        line = get_object_or_404(AuditLine, pk=pk)
        audit = get_visible_audit(request, line.audit_id)
    elif target == "query":
        query = get_object_or_404(Query.objects.select_related("line"), pk=pk)
        audit = query.audit = get_visible_audit(request, query.audit_id)
    else:
        action = get_object_or_404(Action.objects.select_related("query__line"), pk=pk)
        query = action.query
        audit = query.audit = get_visible_audit(request, query.audit_id)
    return {"audit": audit, "line": line, "query": query, "action": action}


def _attachments_of(where: dict):
    return Attachment.objects.filter(
        audit=where["audit"],
        line=where["line"],
        query=where["query"],
        action=where["action"],
        is_deleted=False,
    ).select_related("uploaded_by")


def _panel(request, target: str, pk: int, where: dict, errors=()):
    user = request.user
    attachments = list(_attachments_of(where))
    for attachment in attachments:
        attachment.audit, attachment.query = where["audit"], where["query"]
        attachment.can_remove = can_remove_media(user, attachment)
        attachment.can_tick = can_tick_report(user, attachment)
    return render(
        request,
        "audits/partials/media_panel.html",
        {
            "target": target,
            "target_pk": pk,
            "attachments": attachments,
            "can_add": can_add_media(user, where["audit"], where["query"]),
            "accept": media.ACCEPT,
            "max_files": settings.ATTACHMENT_MAX_FILES,
            "note_max": media.NOTE_MAX,
            "errors": errors,
        },
    )


def _refuse(request, target: str, pk: int, where: dict, reason: str):
    """Show the refusal inside the panel: htmx doesn't swap a 403 page, so the
    user would otherwise see nothing (e.g. the audit was locked a moment ago)."""
    logger.warning("%s refused at %s: %s", request.user.username, request.path, reason)
    return _panel(request, target, pk, where, errors=[reason])


def media_panel(request, target, pk):
    """GET: the attachments of one audit / line / query / action. POST: add files."""
    where = _resolve(request, target, pk)
    if request.method != "POST":
        return _panel(request, target, pk, where)
    if not can_add_media(request.user, where["audit"], where["query"]):
        return _refuse(request, target, pk, where, NO_MEDIA_RIGHT)
    try:
        saved = media.save_attachments(
            request.user,
            request.FILES.getlist("files"),
            note=request.POST.get("note", ""),
            **where,
        )
    except media.MediaError as error:
        logger.info("Media refused on %s %s for %s: %s", target, pk, request.user.username, error)
        return _panel(request, target, pk, where, errors=[str(error)])
    logger.info("%s added %s attachment(s) to %s %s", request.user.username, len(saved), target, pk)
    return _panel(request, target, pk, where)


def _visible_attachment(request, pk) -> Attachment:
    attachment = get_object_or_404(
        Attachment.objects.select_related("query__line", "action", "line"), pk=pk, is_deleted=False
    )
    attachment.audit = get_visible_audit(request, attachment.audit_id)
    if attachment.query_id:
        attachment.query.audit = attachment.audit
    return attachment


def _panel_of(attachment: Attachment) -> tuple[str, int, dict]:
    """The panel an attachment sits in, built from what is already loaded (no new lookups)."""
    where = {
        "audit": attachment.audit,
        "line": attachment.line,
        "query": attachment.query,
        "action": attachment.action,
    }
    if attachment.action_id:
        return "action", attachment.action_id, where
    if attachment.query_id:
        return "query", attachment.query_id, where
    if attachment.line_id:
        return "line", attachment.line_id, where
    return "audit", attachment.audit_id, where


@require_POST
def attachment_remove(request, pk):
    attachment = _visible_attachment(request, pk)
    target, target_pk, where = _panel_of(attachment)
    if not can_remove_media(request.user, attachment):
        return _refuse(
            request,
            target,
            target_pk,
            where,
            "Only the person who added it, or Admin, can remove it, "
            "and only until the audit is Locked (or the query is Closed).",
        )
    media.remove_attachment(request.user, attachment)
    return _panel(request, target, target_pk, where)


@require_POST
def attachment_note(request, pk):
    """Change the note of a photo / file (who added it, or Admin, until Locked)."""
    attachment = _visible_attachment(request, pk)
    target, target_pk, where = _panel_of(attachment)
    if not can_remove_media(request.user, attachment):
        return _refuse(
            request,
            target,
            target_pk,
            where,
            "Only the person who added it, or Admin, can change its note, "
            "and only until the audit is Locked (or the query is Closed).",
        )
    try:
        media.set_note(request.user, attachment, request.POST.get("note", ""))
    except media.MediaError as error:
        logger.info("Note refused on attachment %s: %s", attachment.pk, error)
        return _panel(request, target, target_pk, where, errors=[str(error)])
    return _panel(request, target, target_pk, where)


@require_POST
def attachment_report(request, pk):
    """Tick / untick “include in report” for a photo."""
    attachment = _visible_attachment(request, pk)
    target, target_pk, where = _panel_of(attachment)
    if not can_tick_report(request.user, attachment):
        return _refuse(
            request,
            target,
            target_pk,
            where,
            NO_MEDIA_RIGHT + " Photos for the PDF can't change once the audit is Locked.",
        )
    try:
        media.set_in_report(request.user, attachment, request.POST.get("include") == "on")
    except media.MediaError as error:
        logger.info("Report tick refused on attachment %s: %s", attachment.pk, error)
        return _panel(request, target, target_pk, where, errors=[str(error)])
    return _panel(request, target, target_pk, where)


RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)$")
MAX_RANGE_BYTES = 8 * 1024 * 1024


def attachment_file(request, pk):
    """Open a stored file. Photos and videos show in the browser; other files download.
    Videos answer “Range” requests so phones (Safari) can play and seek them."""
    attachment = _visible_attachment(request, pk)
    try:
        handle = attachment.file.open("rb")
    except FileNotFoundError as error:
        logger.error("Attachment %s missing on disk: %s", attachment.pk, attachment.file.name)
        raise Http404("This file is missing on the server. Ask Admin to restore it.") from error
    is_video = attachment.kind == Attachment.Kind.VIDEO
    match = RANGE_PATTERN.match(request.headers.get("Range", ""))
    if is_video and match and any(match.groups()):
        return _range_response(handle, attachment.size, match, attachment.content_type)
    download_name = attachment.original_name
    if attachment.kind == Attachment.Kind.PHOTO:
        download_name = download_name.rsplit(".", 1)[0] + ".jpg"
    response = FileResponse(
        handle,
        as_attachment=attachment.kind == Attachment.Kind.FILE,
        filename=download_name,
        content_type=attachment.content_type,
    )
    response["Accept-Ranges"] = "bytes" if is_video else "none"
    response["Cache-Control"] = "private, max-age=3600"
    return response


def _range_response(handle, size: int, match, content_type: str) -> HttpResponse:
    start_text, end_text = match.groups()
    if start_text:
        start = int(start_text)
        end = min(int(end_text), size - 1) if end_text else size - 1
    else:  # "bytes=-500" = the last 500 bytes
        start, end = max(size - int(end_text), 0), size - 1
    if start > end or start >= size:
        handle.close()
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{size}"
        return response
    end = min(end, start + MAX_RANGE_BYTES - 1)
    with handle:
        handle.seek(start)
        data = handle.read(end - start + 1)
    response = HttpResponse(data, status=206, content_type=content_type)
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, max-age=3600"
    return response
