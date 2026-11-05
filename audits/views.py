import logging
from datetime import date

from django.contrib import messages
from django.db import transaction
from django.db.models import Case, CharField, Count, Q, Sum, Value, When
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from accounts.access import visible_branches
from audits import erp_import
from audits.forms import AuditForm, CancelForm, FreezeForm, ItemForm, UploadForm
from audits.models import Audit, ErpUpload, Item
from audits.permissions import (
    branches_with_right,
    can_cancel_audit,
    can_edit_audit,
    can_freeze,
    can_manage_items,
    can_upload,
    forbidden,
    get_visible_audit,
)
from core.activity import diff, log_activity, snapshot
from core.datatable import CHOICE, NUMBER, Column, DataTable

logger = logging.getLogger(__name__)

AUDIT_FIELDS = ["branch", "title", "audit_date", "audit_type", "stock_as_on"]
ITEM_FIELDS = ["code", "description", "uom", "is_active", "is_new"]


def _is_htmx(request) -> bool:
    # A history restore (Back button, page not in htmx's cache) needs the whole page.
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


def _vary_on_htmx(response):
    """The same URL gives the whole page or only the table, so caches must keep both apart."""
    patch_vary_headers(response, ["HX-Request"])
    return response


# ---------- Audit list / create / edit ----------


def audit_list(request):
    branches = visible_branches(request.user, request.company)
    audits = (
        Audit.objects.filter(branch__in=branches)
        .select_related("branch")
        .annotate(line_count=Count("lines"), total_value=Sum("lines__erp_value"))
    )
    filters = {key: request.GET.get(key, "") for key in ("branch", "stage", "type", "from", "to")}
    if filters["branch"].isdigit():
        audits = audits.filter(branch_id=filters["branch"])
    if filters["stage"] in Audit.Stage.values:
        audits = audits.filter(stage=filters["stage"])
    if filters["type"] in Audit.Type.values:
        audits = audits.filter(audit_type=filters["type"])
    for key, lookup in (("from", "audit_date__gte"), ("to", "audit_date__lte")):
        try:
            audits = audits.filter(**{lookup: date.fromisoformat(filters[key])})
        except ValueError:
            filters[key] = ""
    return render(
        request,
        "audits/audit_list.html",
        {
            "audits": audits,
            "branches": branches,
            "filters": filters,
            "stages": Audit.Stage.choices,
            "types": Audit.Type.choices,
            "can_create": bool(branches_with_right(request.user, request.company, "upload_erp")),
        },
    )


def audit_create(request):
    branches = branches_with_right(request.user, request.company, "upload_erp")
    if not branches:
        return forbidden(request, "You need the “Upload ERP” right in a Branch to create audits.")
    today = timezone.localdate()
    initial = {
        "audit_date": today,
        "stock_as_on": today,
        "title": today.strftime("%b%y"),
        "branch": branches[0].pk if len(branches) == 1 else None,
    }
    form = AuditForm(request.POST or None, branches=branches, initial=initial)
    if request.method == "POST" and form.is_valid():
        audit = form.save(commit=False)
        audit.created_by = request.user
        audit.save()
        log_activity(request.user, "create", audit, diff({}, snapshot(audit, AUDIT_FIELDS)))
        messages.success(request, f"Audit “{audit.title}” created. Now upload the ERP stock file.")
        return redirect("audits:audit_detail", pk=audit.pk)
    return render(request, "audits/audit_form.html", {"form": form, "audit": None})


def audit_edit(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_edit_audit(request.user, audit):
        return forbidden(
            request, "Only the creator or Admin can edit an audit, and only in ERP Draft."
        )
    before = snapshot(audit, AUDIT_FIELDS)
    form = AuditForm(request.POST or None, instance=audit)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        changes = diff(before, snapshot(saved, AUDIT_FIELDS))
        if changes:
            log_activity(request.user, "update", saved, changes)
        messages.success(request, f"Audit “{saved.title}” saved.")
        return redirect("audits:audit_detail", pk=saved.pk)
    return render(request, "audits/audit_form.html", {"form": form, "audit": audit})


# ---------- Audit page ----------


LINE_COLUMNS = [
    Column("item_code", "Item No"),
    Column("description", "Description"),
    Column("uom", "UoM", type=CHOICE),
    Column("erp_qty", "ERP Qty", type=NUMBER),
    Column("rate", "Rate p.u.", type=NUMBER),
    Column("erp_value", "ERP value", type=NUMBER),
]


def _lines_table(request, audit) -> DataTable:
    return DataTable(
        request.GET,
        audit.lines.all(),
        LINE_COLUMNS,
        table_id="audit-lines",
        default_sort=("pk",),
        search_fields=("item_code", "description"),
        search_placeholder="Search item code or description",
        base_url=request.path,
        noun="lines",
    )


def audit_detail(request, pk):
    audit = get_visible_audit(request, pk)
    table = _lines_table(request, audit)
    if table.wants_csv:
        return table.csv_response(f"{audit.title}_erp_lines_{timezone.localdate().isoformat()}.csv")
    if _is_htmx(request):
        return _vary_on_htmx(
            render(request, "audits/partials/audit_lines.html", {"audit": audit, "table": table})
        )
    totals = audit.lines.aggregate(count=Count("id"), value=Sum("erp_value"))
    user = request.user
    return _vary_on_htmx(
        render(
            request,
            "audits/audit_detail.html",
            {
                "audit": audit,
                "table": table,
                "totals": totals,
                "uploads": audit.uploads.select_related("uploaded_by"),
                "upload_form": UploadForm(),
                "can_edit": can_edit_audit(user, audit),
                "can_upload": can_upload(user, audit),
                "can_freeze": can_freeze(user, audit) and totals["count"] > 0,
                "can_cancel": can_cancel_audit(user, audit),
            },
        )
    )


# ---------- ERP template and upload ----------


def erp_template(request):
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="erp_stock_template.xlsx"'
    erp_import.template_workbook().save(response)
    return response


@require_POST
def erp_upload(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_upload(request.user, audit):
        return forbidden(
            request, "You need the “Upload ERP” right, and the audit must still be in ERP Draft."
        )
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a file to upload.")
        return redirect("audits:audit_detail", pk=audit.pk)
    uploaded = form.cleaned_data["file"]
    check = erp_import.check_file(uploaded, uploaded.name)
    if check.has_errors:
        # Nothing is saved: show the errors; the user fixes the file and uploads again.
        logger.info(
            "ERP file %s for audit %s has %s errors", uploaded.name, audit.pk, check.error_count
        )
        return _render_upload_check(request, audit, check, uploaded.name, upload=None)
    upload = ErpUpload.objects.create(
        audit=audit,
        rows=erp_import.rows_to_json(check),
        original_name=uploaded.name[:255],
        uploaded_by=request.user,
        line_count=len(check.rows),
        total_value=check.total_value,
    )
    return redirect("audits:upload_preview", pk=upload.pk)


def _render_upload_check(request, audit, check, file_name: str, upload):
    new_codes = {row.code.lower() for row in check.rows} - {
        code.lower()
        for code in Item.objects.filter(company=audit.branch.company).values_list("code", flat=True)
    }
    return render(
        request,
        "audits/upload_preview.html",
        {
            "audit": audit,
            "upload": upload,
            "file_name": file_name,
            "check": check,
            "new_item_count": len(new_codes),
            "can_confirm": upload is not None and can_upload(request.user, audit),
        },
    )


def upload_preview(request, pk):
    upload = get_object_or_404(ErpUpload, pk=pk)
    audit = get_visible_audit(request, upload.audit_id)
    if upload.status != ErpUpload.Status.PENDING:
        messages.info(
            request,
            f"“{upload.original_name}” was already {upload.get_status_display().lower()}. "
            "To change the ERP Draft, upload a new file.",
        )
        return redirect("audits:audit_detail", pk=audit.pk)
    if request.method == "POST":
        if not can_upload(request.user, audit):
            return forbidden(request, "You can no longer change the ERP file of this audit.")
        try:
            erp_import.confirm_upload(
                request.user, upload, accept_warnings=request.POST.get("accept_warnings") == "on"
            )
        except erp_import.UploadNotAllowed as error:
            logger.info("ERP upload %s not confirmed: %s", upload.pk, error)
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                f"ERP Draft saved: {upload.line_count:,} lines, total value {upload.total_value:,}.",
            )
            return redirect("audits:audit_detail", pk=audit.pk)
    return _render_upload_check(
        request, audit, erp_import.saved_check(upload), upload.original_name, upload
    )


# ---------- Tolerance + freeze ----------


def audit_freeze(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_freeze(request.user, audit):
        return forbidden(
            request, "You need the “Tolerance & freeze” right, and the audit must be in ERP Draft."
        )
    lines = audit.lines.all()
    summary = lines.aggregate(
        count=Count("id"),
        value=Sum("erp_value"),
        negative=Count("id", filter=Q(erp_qty__lt=0)),
        new_items=Count("id", filter=Q(item__is_new=True)),
    )
    if not summary["count"]:
        messages.error(request, "Upload and confirm the ERP file before freezing.")
        return redirect("audits:audit_detail", pk=audit.pk)
    before = snapshot(audit, ["qty_tolerance", "pct_tolerance", "stage"])
    form = FreezeForm(request.POST or None, instance=audit)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked = Audit.objects.select_for_update().get(pk=audit.pk)
            if locked.stage != Audit.Stage.ERP_DRAFT:
                messages.error(request, "This audit was already frozen or cancelled.")
                return redirect("audits:audit_detail", pk=audit.pk)
            frozen = form.save(commit=False)
            frozen.stage = Audit.Stage.ERP_FROZEN
            frozen.frozen_by = request.user
            frozen.frozen_at = timezone.now()
            frozen.save(
                update_fields=["qty_tolerance", "pct_tolerance", "stage", "frozen_by", "frozen_at"]
            )
            log_activity(
                request.user,
                "freeze ERP stock",
                frozen,
                diff(before, snapshot(frozen, ["qty_tolerance", "pct_tolerance", "stage"])),
            )
        messages.success(request, "ERP stock frozen. Counting can start.")
        return redirect("audits:audit_detail", pk=audit.pk)
    return render(
        request, "audits/audit_freeze.html", {"audit": audit, "form": form, "summary": summary}
    )


# ---------- Cancel ----------


def audit_cancel(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_cancel_audit(request.user, audit):
        return forbidden(
            request,
            "Admin can cancel an audit in ERP Draft or ERP Frozen; the creator only in ERP Draft.",
        )
    form = CancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        old_stage = audit.stage
        audit.stage = Audit.Stage.CANCELLED
        audit.cancelled_by = request.user
        audit.cancelled_at = timezone.now()
        audit.cancel_reason = form.cleaned_data["reason"]
        audit.save(update_fields=["stage", "cancelled_by", "cancelled_at", "cancel_reason"])
        log_activity(
            request.user,
            "cancel",
            audit,
            {"stage": [old_stage, audit.stage], "reason": [None, audit.cancel_reason]},
        )
        messages.success(request, f"Audit “{audit.title}” cancelled.")
        return redirect("audits:audit_detail", pk=audit.pk)
    return render(request, "audits/audit_cancel.html", {"audit": audit, "form": form})


# ---------- Items ----------


ITEM_STATUS = Case(
    When(is_active=False, then=Value("inactive")),
    When(is_new=True, then=Value("new")),
    default=Value("active"),
    output_field=CharField(),
)
ITEM_STATUS_LABELS = {"new": "New (not reviewed)", "active": "Active", "inactive": "Inactive"}
ITEM_COLUMNS = [
    Column("code", "Item code"),
    Column("description", "Description"),
    Column("uom", "UoM", type=CHOICE),
    Column("status", "Status", type=CHOICE, choices=ITEM_STATUS_LABELS),
]
ITEM_SHORTCUTS = [
    ("All", "status", "", "Show every item"),
    ("New", "status", "new", "Items added from an ERP upload and not reviewed yet"),
    ("Active", "status", "active", "Reviewed items in use"),
    ("Inactive", "status", "inactive", "Items turned off; nothing is deleted"),
]


def _item_table(company, params, base_url="") -> DataTable:
    return DataTable(
        params,
        Item.objects.filter(company=company).annotate(status=ITEM_STATUS),
        ITEM_COLUMNS,
        table_id="item-table",
        default_sort=("code",),
        search_fields=("code", "description"),
        search_placeholder="Search code or description",
        shortcuts=ITEM_SHORTCUTS,
        base_url=base_url,
        noun="items",
    )


def item_list(request):
    company = request.company
    if company is None or not visible_branches(request.user, company).exists():
        return forbidden(request, "You need access to a Branch of this company to see its items.")
    table = _item_table(company, request.GET, request.path)
    if table.wants_csv:
        return table.csv_response(
            f"items_{slugify(company.name)}_{timezone.localdate().isoformat()}.csv"
        )
    can_manage = can_manage_items(request.user, company)
    context = {
        "table": table,
        "new_count": Item.objects.filter(company=company, is_new=True).count(),
        "matching_new_count": table.filtered.filter(is_new=True).count() if can_manage else 0,
        "can_manage": can_manage,
    }
    template = "audits/partials/item_table.html" if _is_htmx(request) else "audits/item_list.html"
    return _vary_on_htmx(render(request, template, context))


def _posted_ids(request, name: str) -> list[int]:
    return [
        int(value) for value in request.POST.getlist(name) if value.isdecimal() and value.isascii()
    ]


@require_POST
def item_bulk_review(request):
    """Mark many New items Reviewed: ticked rows, or every item matching the search/filter."""
    company = request.company
    if not can_manage_items(request.user, company):
        return forbidden(request, "Only Admin or users with “Tolerance & freeze” can edit items.")
    # The list's search and column filters, as they were when the user ticked "select all".
    filters = _item_table(company, QueryDict(request.POST.get("filters", "")))
    if request.POST.get("all_matching") == "1":
        items = filters.filtered.exclude(pk__in=_posted_ids(request, "exclude"))
    else:
        items = Item.objects.filter(company=company, pk__in=_posted_ids(request, "ids"))
    list_url = f"{reverse('audits:item_list')}?{filters.filter_query}"
    with transaction.atomic():
        reviewed = list(items.filter(is_new=True))  # someone else may have reviewed some already
        expected = request.POST.get("expected", "")
        if expected.isdecimal() and len(reviewed) > int(expected):
            # The list grew after the user confirmed (e.g. a new ERP upload): don't touch extra items.
            messages.error(
                request,
                f"The list changed: {len(reviewed):,} items now match, not {int(expected):,}. "
                "Nothing was changed. Check the list and try again.",
            )
            logger.info("Bulk review refused: %s matching, %s confirmed", len(reviewed), expected)
            return redirect(list_url)
        # is_new=True again here so an item reviewed by someone else meanwhile is not logged twice.
        Item.objects.filter(pk__in=[item.pk for item in reviewed], is_new=True).update(is_new=False)
        for item in reviewed:
            log_activity(request.user, "review", item, {"is_new": [True, False]})
    if reviewed:
        messages.success(
            request, f"{len(reviewed):,} item{'s' if len(reviewed) != 1 else ''} marked Reviewed."
        )
    else:
        messages.info(request, "No New items were selected, so nothing changed.")
    return redirect(list_url)


def item_form(request, pk=None):
    company = request.company
    if not can_manage_items(request.user, company):
        return forbidden(request, "Only Admin or users with “Tolerance & freeze” can edit items.")
    item = get_object_or_404(Item, pk=pk, company=company) if pk else None
    before = snapshot(item, ITEM_FIELDS) if item else {}
    form = ItemForm(request.POST or None, instance=item, company=company)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.company = company
        saved.save()
        after = snapshot(saved, ITEM_FIELDS)
        changes = diff(before, after)
        if item is None:
            log_activity(request.user, "create", saved, changes)
        elif changes:
            log_activity(request.user, "update", saved, changes)
        messages.success(request, f"Item “{saved.code}” saved.")
        return redirect("audits:item_list")
    return render(request, "audits/item_form.html", {"form": form, "item": item})


def _item_change(request, pk, field_name: str, value=None):
    company = request.company
    if not can_manage_items(request.user, company):
        return forbidden(request, "Only Admin or users with “Tolerance & freeze” can edit items.")
    item = get_object_or_404(Item, pk=pk, company=company)
    old = getattr(item, field_name)
    new = (not old) if value is None else value
    if old != new:
        setattr(item, field_name, new)
        item.save(update_fields=[field_name])
        action = {"is_new": "review", "is_active": "activate" if new else "deactivate"}[field_name]
        log_activity(request.user, action, item, {field_name: [old, new]})
    return render(request, "audits/partials/item_row.html", {"item": item, "can_manage": True})


@require_POST
def item_toggle(request, pk):
    return _item_change(request, pk, "is_active")


@require_POST
def item_review(request, pk):
    return _item_change(request, pk, "is_new", value=False)
