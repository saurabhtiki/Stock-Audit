"""Screens for Phase 3: count a line, count sheet, remarks, Checking Done."""

import logging

from django.contrib import messages
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audits import counting
from audits.forms import CheckingDoneForm, RemarkForm, UploadForm
from audits.models import AuditLine, CountUpload, Remark
from audits.permissions import (
    can_change_remark,
    can_count,
    can_remark,
    forbidden,
    get_visible_audit,
)
from core.activity import log_activity

logger = logging.getLogger(__name__)

NO_COUNT_RIGHT = "You need the “Count” right in this Branch, and the audit must be ERP Frozen."
NO_REMARK_RIGHT = (
    "You need the “Count” right in this Branch, and the audit must be ERP Frozen or Checking Done."
)


def attach_remark_counts(lines) -> None:
    """Set `remark_count` on each line object (one query for the whole page)."""
    counts = dict(
        Remark.objects.filter(line__in=[line.pk for line in lines], is_deleted=False)
        .values("line")
        .annotate(total=Count("id"))
        .values_list("line", "total")
    )
    for line in lines:
        line.remark_count = counts.get(line.pk, 0)


# ---------- Count one line ----------


@require_POST
def line_count(request, pk):
    line = get_object_or_404(AuditLine.objects.select_related("counted_by"), pk=pk)
    audit = get_visible_audit(request, line.audit_id)
    raw = request.POST.get("physical_qty", "")
    context = {"audit": audit, "can_count": True, "can_remark": can_remark(request.user, audit)}
    if not can_count(request.user, audit):
        if request.headers.get("HX-Request") != "true":
            return forbidden(request, NO_COUNT_RIGHT)
        # Shown in the row: htmx would silently drop a 403 page (e.g. Checking Done meanwhile).
        logger.info("Count for line %s refused for %s: not allowed", pk, request.user.username)
        line.typed = raw
        attach_remark_counts([line])
        context.update(line=line, error="Not saved. " + NO_COUNT_RIGHT + " Reload the page.")
        return render(request, "audits/partials/count_row.html", context)
    try:
        line, changed = counting.save_count(request.user, line.pk, raw)
    except counting.CountError as error:
        logger.info("Count for line %s refused: %s", pk, error)
        line.typed = raw
        attach_remark_counts([line])
        context.update(line=line, error=str(error))
        return render(request, "audits/partials/count_row.html", context)
    attach_remark_counts([line])
    context.update(line=line, saved=True)
    response = render(request, "audits/partials/count_row.html", context)
    if changed:
        response["HX-Trigger"] = "counts-changed"
    return response


def count_stats(request, pk):
    audit = get_visible_audit(request, pk)
    return render(
        request,
        "audits/partials/count_stats.html",
        {"audit": audit, "summary": counting.count_summary(audit)},
    )


# ---------- Count sheet ----------


def count_sheet(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_count(request.user, audit):
        return forbidden(request, NO_COUNT_RIGHT)
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    name = f"count_sheet_{audit.branch.code}_{audit.title}.xlsx".replace(" ", "_")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    counting.sheet_workbook(audit).save(response)
    log_activity(request.user, "download count sheet", audit)
    return response


@require_POST
def count_sheet_upload(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_count(request.user, audit):
        return forbidden(request, NO_COUNT_RIGHT)
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a count sheet to upload.")
        return redirect("audits:audit_detail", pk=audit.pk)
    uploaded = form.cleaned_data["file"]
    check = counting.check_sheet(audit, uploaded, uploaded.name)
    if check.has_errors:
        logger.info(
            "Count sheet %s for audit %s has %s errors", uploaded.name, audit.pk, check.error_count
        )
        return _render_sheet_check(request, audit, check, uploaded.name, upload=None)
    upload = CountUpload.objects.create(
        audit=audit,
        rows=counting.sheet_rows_to_json(check),
        original_name=uploaded.name[:255],
        uploaded_by=request.user,
    )
    return redirect("audits:count_sheet_preview", pk=upload.pk)


def _render_sheet_check(request, audit, check, file_name, upload):
    return render(
        request,
        "audits/count_sheet_preview.html",
        {
            "audit": audit,
            "upload": upload,
            "file_name": file_name,
            "check": check,
            "can_confirm": upload is not None and can_count(request.user, audit),
        },
    )


def _posted_int(request, name: str) -> int:
    raw = request.POST.get(name, "")
    return int(raw) if raw.isdecimal() and raw.isascii() else 0


def count_sheet_preview(request, pk):
    upload = get_object_or_404(CountUpload, pk=pk)
    audit = get_visible_audit(request, upload.audit_id)
    if upload.status != CountUpload.Status.PENDING:
        messages.info(request, f"“{upload.original_name}” was already saved.")
        return redirect("audits:audit_detail", pk=audit.pk)
    if request.method == "POST":
        if not can_count(request.user, audit):
            return forbidden(request, NO_COUNT_RIGHT)
        try:
            result = counting.confirm_sheet(
                request.user,
                upload,
                accept_warnings=request.POST.get("accept_warnings") == "on",
                expected_changes=_posted_int(request, "expected_changes"),
            )
        except counting.CountError as error:
            logger.info("Count sheet %s not saved: %s", upload.pk, error)
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                f"Count sheet saved: {result['counts']:,} counts changed, "
                f"{result['remarks']:,} remarks added.",
            )
            return redirect("audits:audit_detail", pk=audit.pk)
    return _render_sheet_check(
        request, audit, counting.saved_sheet(upload), upload.original_name, upload
    )


# ---------- Remarks ----------


def _panel(request, audit, line, form=None, error=""):
    remarks = Remark.objects.filter(audit=audit, line=line, is_deleted=False).select_related(
        "created_by"
    )
    user = request.user
    for remark in remarks:
        remark.audit = audit
        remark.can_change = can_change_remark(user, remark)
    return render(
        request,
        "audits/partials/remark_panel.html",
        {
            "audit": audit,
            "line": line,
            "remarks": remarks,
            "form": form or RemarkForm(),
            "can_remark": can_remark(user, audit),
            "error": error,
        },
    )


def _line_of(audit, line_id):
    if not line_id:
        return None
    if not str(line_id).isdecimal():
        raise Http404("Line not found.")
    return get_object_or_404(AuditLine, pk=line_id, audit=audit)


def remark_panel(request, pk):
    """GET: the remarks of a line (?line=) or of the whole audit. POST: add one."""
    audit = get_visible_audit(request, pk)
    line = _line_of(audit, request.GET.get("line") or request.POST.get("line"))
    if request.method != "POST":
        return _panel(request, audit, line)
    if not can_remark(request.user, audit):
        return forbidden(request, NO_REMARK_RIGHT)
    form = RemarkForm(request.POST)
    if not form.is_valid():
        logger.info("Remark not added on audit %s: %s", audit.pk, form.errors.as_text())
        return _panel(request, audit, line, form=form)
    remark = Remark.objects.create(
        audit=audit, line=line, text=form.cleaned_data["text"], created_by=request.user
    )
    remark.audit = audit
    log_activity(request.user, "add remark", remark, {"text": [None, remark.text]})
    return _panel(request, audit, line)


def _own_remark(request, pk):
    remark = get_object_or_404(Remark.objects.select_related("line"), pk=pk, is_deleted=False)
    audit = get_visible_audit(request, remark.audit_id)
    remark.audit = audit
    return remark, audit


def _refuse_remark(request, audit, remark, reason: str):
    if request.headers.get("HX-Request") != "true":
        return forbidden(request, reason)
    logger.info("Remark %s change refused for %s", remark.pk, request.user.username)
    return _panel(request, audit, remark.line, error=reason)


@require_POST
def remark_edit(request, pk):
    remark, audit = _own_remark(request, pk)
    if not can_change_remark(request.user, remark):
        return _refuse_remark(
            request,
            audit,
            remark,
            "Only the person who wrote a remark can change it, until Locked.",
        )
    form = RemarkForm(request.POST)
    if not form.is_valid():
        logger.info("Remark %s not edited: %s", remark.pk, form.errors.as_text())
        return _panel(request, audit, remark.line, error=form.errors["text"][0])
    old = remark.text
    if form.cleaned_data["text"] != old:
        remark.text = form.cleaned_data["text"]
        remark.edited_at = timezone.now()
        remark.save(update_fields=["text", "edited_at"])
        log_activity(request.user, "edit remark", remark, {"text": [old, remark.text]})
    return _panel(request, audit, remark.line)


@require_POST
def remark_delete(request, pk):
    remark, audit = _own_remark(request, pk)
    if not can_change_remark(request.user, remark):
        return _refuse_remark(
            request,
            audit,
            remark,
            "Only the person who wrote a remark can delete it, until Locked.",
        )
    remark.is_deleted = True
    remark.deleted_at = timezone.now()
    remark.save(update_fields=["is_deleted", "deleted_at"])
    log_activity(request.user, "delete remark", remark, {"text": [remark.text, None]})
    return _panel(request, audit, remark.line)


# ---------- Checking Done ----------


def checking_done(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_count(request.user, audit):
        return forbidden(request, NO_COUNT_RIGHT)
    summary = counting.count_summary(audit)
    form = CheckingDoneForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            queries = counting.mark_checking_done(request.user, audit.pk)
        except counting.CountError as error:
            logger.info("Checking Done refused for audit %s: %s", audit.pk, error)
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                f"Checking Done. {queries:,} quer{'ies' if queries != 1 else 'y'} raised. "
                "Counts are now fixed; remarks can still be changed until Locked.",
            )
            return redirect("audits:audit_detail", pk=audit.pk)
    return render(
        request, "audits/checking_done.html", {"audit": audit, "form": form, "summary": summary}
    )
