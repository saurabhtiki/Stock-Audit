import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.db.models import Count
from django.http import JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_POST

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


@admin_required
def company_list(request):
    companies = Company.objects.annotate(branch_count=Count("branches"))
    return render(request, "core/company_list.html", {"companies": companies})


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
    company = get_object_or_404(Company, pk=pk)
    company.is_active = not company.is_active
    company.save(update_fields=["is_active"])
    action = "activate" if company.is_active else "deactivate"
    log_activity(
        request.user, action, company, {"is_active": [not company.is_active, company.is_active]}
    )
    company.branch_count = company.branches.count()
    return render(request, "core/partials/company_row.html", {"company": company})


# ---------- Branches ----------


@admin_required
def branch_list(request):
    branches = Branch.objects.select_related("company")
    company_id = request.GET.get("company", "")
    if company_id.isdigit():
        branches = branches.filter(company_id=company_id)
    return render(
        request,
        "core/branch_list.html",
        {"branches": branches, "companies": Company.objects.all(), "company_id": company_id},
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
        return redirect(f"{reverse('core:branch_list')}?company={saved.company_id}")
    return render(request, "core/branch_form.html", {"form": form, "branch": branch})


@admin_required
@require_POST
def branch_toggle(request, pk):
    branch = get_object_or_404(Branch.objects.select_related("company"), pk=pk)
    branch.is_active = not branch.is_active
    branch.save(update_fields=["is_active"])
    action = "activate" if branch.is_active else "deactivate"
    log_activity(
        request.user, action, branch, {"is_active": [not branch.is_active, branch.is_active]}
    )
    return render(request, "core/partials/branch_row.html", {"branch": branch})


# ---------- Departments ----------


@admin_required
def department_list(request):
    departments = Department.objects.select_related("company")
    company_id = request.GET.get("company", "")
    if company_id.isdigit():
        departments = departments.filter(company_id=company_id)
    return render(
        request,
        "core/department_list.html",
        {"departments": departments, "companies": Company.objects.all(), "company_id": company_id},
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
        return redirect(f"{reverse('core:department_list')}?company={saved.company_id}")
    return render(request, "core/department_form.html", {"form": form, "department": department})


@admin_required
@require_POST
def department_toggle(request, pk):
    department = get_object_or_404(Department.objects.select_related("company"), pk=pk)
    department.is_active = not department.is_active
    department.save(update_fields=["is_active"])
    action = "activate" if department.is_active else "deactivate"
    log_activity(
        request.user,
        action,
        department,
        {"is_active": [not department.is_active, department.is_active]},
    )
    return render(request, "core/partials/department_row.html", {"department": department})


# ---------- Activity log ----------


@admin_required
def activity_log(request):
    entries = ActivityLog.objects.select_related("actor")
    record_type = request.GET.get("type", "")
    if record_type:
        entries = entries.filter(record_type=record_type)
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    record_types = (
        ActivityLog.objects.order_by("record_type").values_list("record_type", flat=True).distinct()
    )
    return render(
        request,
        "core/activity_log.html",
        {"page": page, "record_types": record_types, "record_type": record_type},
    )
