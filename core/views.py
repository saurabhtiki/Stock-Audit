import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connection
from django.db.models import Count, F, TextField, Value
from django.db.models.functions import Cast, Coalesce
from django.http import JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_POST
from django_datatable import (
    ACTIVE_INACTIVE,
    CHOICE,
    DATE,
    NUMBER,
    Column,
    DataTable,
    table_response,
)

from accounts.access import visible_branches
from accounts.middleware import SESSION_COMPANY_KEY
from core.activity import diff, log_activity, snapshot
from core.forms import BranchForm, CompanyForm, DepartmentForm
from core.models import ActivityLog, Branch, Company, Department
from core.permissions import admin_required

logger = logging.getLogger(__name__)

COMPANY_FIELDS = ["name", "address", "logo", "is_active"]
BRANCH_FIELDS = ["company", "name", "code", "is_active"]
DEPARTMENT_FIELDS = ["company", "name", "is_active"]


@login_not_required
def health(request):
    """Quick check that the app and database are up."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        logger.exception("Health check: database not reachable")
        return JsonResponse({"status": "error", "detail": "Database is not reachable."}, status=503)
    return JsonResponse({"status": "ok"})


def home(request):
    from audits.pending_views import pending_section  # audits depends on core, not the reverse

    context = {"branches": visible_branches(request.user, request.company)}
    if request.company:
        pending = pending_section(request)
        if isinstance(pending, HttpResponseBase):  # CSV download or an htmx table swap
            return pending
        context.update(pending)
    response = render(request, "core/home.html", context)
    patch_vary_headers(response, ["HX-Request", "HX-Target"])
    return response


@require_POST
def switch_company(request):
    wanted = request.POST.get("company_id", "")
    company = next((c for c in request.companies if str(c.pk) == wanted), None)
    if company is None:
        logger.warning("%s tried to switch to company %r", request.user.username, wanted)
        messages.error(request, "You do not have access to that company.")
    else:
        request.session[SESSION_COMPANY_KEY] = company.pk
        messages.success(request, f"You are now working in {company.name}.")
    return redirect("core:home")


# ---------- Companies ----------


def _toggle_active(request, record, row_template: str):
    """Activate / deactivate one record (nothing is ever deleted), log it, redraw its row."""
    record.is_active = not record.is_active
    record.save(update_fields=["is_active"])
    action = "activate" if record.is_active else "deactivate"
    log_activity(
        request.user, action, record, {"is_active": [not record.is_active, record.is_active]}
    )
    return render(request, row_template, {"row": record})


def _picked_company_id(request) -> str:
    """The company to pre-fill on “Add …” when the list is filtered to exactly one company."""
    names = [name for name in request.GET.getlist("company_name") if name]
    if len(names) != 1:
        return ""
    company = Company.objects.filter(name=names[0]).first()
    return str(company.pk) if company else ""


def _company_list_url(list_name: str, company) -> str:
    """A list filtered to one company, e.g. the Branches of Alpha Traders."""
    return f"{reverse(list_name)}?{urlencode({'company_name': company.name})}"


def _company_rows():
    return Company.objects.annotate(branch_count=Count("branches"))


COMPANY_COLUMNS = [
    Column("name", "Company"),
    Column("address", "Address"),
    Column("branch_count", "Branches", type=NUMBER),
    Column("is_active", "Status", type=CHOICE, choices=ACTIVE_INACTIVE),
]


@admin_required
def company_list(request):
    table = DataTable(
        request.GET,
        _company_rows(),
        COMPANY_COLUMNS,
        table_id="company-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
        default_sort=("name",),
        base_url=request.path,
        noun="companies",
    )
    return table_response(
        request,
        table,
        "core/company_list.html",
        "core/partials/company_table.html",
        {"table": table},
    )


@admin_required
def company_form(request, pk=None):
    company = get_object_or_404(Company, pk=pk) if pk else None
    before = snapshot(company, COMPANY_FIELDS) if company else {}
    form = CompanyForm(request.POST or None, request.FILES or None, instance=company)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        if company:
            changes = diff(before, snapshot(saved, COMPANY_FIELDS))
            if changes:
                log_activity(request.user, "update", saved, changes)
            messages.success(request, f"Company “{saved.name}” saved.")
        else:
            log_activity(request.user, "create", saved, diff({}, snapshot(saved, COMPANY_FIELDS)))
            messages.success(request, f"Company “{saved.name}” added. Now add its Branches.")
        return redirect("core:company_list")
    return render(request, "core/company_form.html", {"form": form, "company": company})


@admin_required
@require_POST
def company_toggle(request, pk):
    company = get_object_or_404(_company_rows(), pk=pk)
    return _toggle_active(request, company, "core/partials/company_row.html")


# ---------- Branches ----------


BRANCH_COLUMNS = [
    Column("code", "Code"),
    Column("name", "Branch"),
    Column("company_name", "Company", type=CHOICE),
    Column("is_active", "Status", type=CHOICE, choices=ACTIVE_INACTIVE),
]


@admin_required
def branch_list(request):
    table = DataTable(
        request.GET,
        Branch.objects.select_related("company").annotate(company_name=F("company__name")),
        BRANCH_COLUMNS,
        table_id="branch-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
        default_sort=("company__name", "code"),
        search_fields=("code", "name"),
        search_placeholder="Search Branch code or name",
        base_url=request.path,
        noun="Branches",
    )
    context = {"table": table, "company_id": _picked_company_id(request)}
    return table_response(
        request, table, "core/branch_list.html", "core/partials/branch_table.html", context
    )


@admin_required
def branch_form(request, pk=None):
    branch = get_object_or_404(Branch, pk=pk) if pk else None
    before = snapshot(branch, BRANCH_FIELDS) if branch else {}
    initial = {"company": request.GET.get("company")} if not branch else None
    form = BranchForm(request.POST or None, instance=branch, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        after = snapshot(saved, BRANCH_FIELDS)
        if branch:
            changes = diff(before, after)
            if changes:
                log_activity(request.user, "update", saved, changes)
        else:
            log_activity(request.user, "create", saved, diff({}, after))
        messages.success(request, f"Branch “{saved}” saved.")
        return redirect(_company_list_url("core:branch_list", saved.company))
    return render(request, "core/branch_form.html", {"form": form, "branch": branch})


@admin_required
@require_POST
def branch_toggle(request, pk):
    branch = get_object_or_404(Branch.objects.select_related("company"), pk=pk)
    return _toggle_active(request, branch, "core/partials/branch_row.html")


# ---------- Departments ----------


DEPARTMENT_COLUMNS = [
    Column("name", "Department"),
    Column("company_name", "Company", type=CHOICE),
    Column("is_active", "Status", type=CHOICE, choices=ACTIVE_INACTIVE),
]


@admin_required
def department_list(request):
    table = DataTable(
        request.GET,
        Department.objects.select_related("company").annotate(company_name=F("company__name")),
        DEPARTMENT_COLUMNS,
        table_id="department-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
        default_sort=("company__name", "name"),
        base_url=request.path,
        noun="departments",
    )
    context = {"table": table, "company_id": _picked_company_id(request)}
    return table_response(
        request,
        table,
        "core/department_list.html",
        "core/partials/department_table.html",
        context,
    )


@admin_required
def department_form(request, pk=None):
    department = get_object_or_404(Department, pk=pk) if pk else None
    before = snapshot(department, DEPARTMENT_FIELDS) if department else {}
    initial = {"company": request.GET.get("company")} if not department else None
    form = DepartmentForm(request.POST or None, instance=department, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        after = snapshot(saved, DEPARTMENT_FIELDS)
        if department:
            changes = diff(before, after)
            if changes:
                log_activity(request.user, "update", saved, changes)
        else:
            log_activity(request.user, "create", saved, diff({}, after))
        messages.success(request, f"Department “{saved.name}” saved.")
        return redirect(_company_list_url("core:department_list", saved.company))
    return render(request, "core/department_form.html", {"form": form, "department": department})


@admin_required
@require_POST
def department_toggle(request, pk):
    department = get_object_or_404(Department.objects.select_related("company"), pk=pk)
    return _toggle_active(request, department, "core/partials/department_row.html")


# ---------- Activity log ----------


def changes_text(entry) -> str:
    """The changes of one log row as readable text, e.g. “is_active: True → False”."""
    parts = []
    for field, values in (entry.changes or {}).items():
        if isinstance(values, (list, tuple)) and len(values) == 2:
            old, new = ("—" if value is None else value for value in values)
            parts.append(f"{field}: {old} → {new}")
        else:
            parts.append(f"{field}: {values}")
    return "; ".join(parts)


ACTIVITY_COLUMNS = [
    Column("created_at", "When", type=DATE),
    Column("actor_name", "Who", type=CHOICE),
    Column("action", "Did", type=CHOICE),
    Column("record_type", "Record type", type=CHOICE),
    Column("record_label", "Record"),
    Column("changes_text", "Old → new", sortable=False, csv_value=changes_text),
]


@admin_required
def activity_log(request):
    entries = ActivityLog.objects.select_related("actor").annotate(
        actor_name=Coalesce(F("actor__username"), Value("system")),
        changes_text=Cast("changes", TextField()),  # lets the text filter search old / new values
    )
    table = DataTable(
        request.GET,
        entries,
        ACTIVITY_COLUMNS,
        table_id="activity-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
        default_sort=("-created_at", "-id"),
        search_fields=("record_label", "changes_text"),
        search_placeholder="Search record or old / new values",
        base_url=request.path,
        noun="log rows",
        default_per_page=50,
    )
    return table_response(
        request,
        table,
        "core/activity_log.html",
        "core/partials/activity_table.html",
        {"table": table},
    )
