"""Admin screens: users, company roles and Branch rights."""

import logging

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.forms import (
    MembershipAddForm,
    PasswordResetByAdminForm,
    ProfilePhotoForm,
    UserCreateForm,
    UserEditForm,
)
from accounts.models import Membership, User
from accounts.rights import RIGHT_NAMES, RIGHTS, Role
from accounts.rights_service import copy_rights, rights_grid, save_rights
from core.activity import diff, log_activity, snapshot
from core.models import Department
from core.permissions import admin_required

logger = logging.getLogger(__name__)

USER_FIELDS = ["username", "full_name", "email", "is_admin", "is_active"]


@admin_required
def user_list(request):
    users = User.objects.prefetch_related("memberships__company")
    search = request.GET.get("q", "").strip()
    if search:
        users = users.filter(
            Q(username__icontains=search)
            | Q(full_name__icontains=search)
            | Q(email__icontains=search)
        )
    is_htmx = request.headers.get("HX-Request") == "true"
    template = "accounts/partials/user_rows.html" if is_htmx else "accounts/user_list.html"
    return render(request, template, {"users": users, "search": search})


@admin_required
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        user.set_password(form.cleaned_data["temporary_password"])
        user.must_change_password = True
        user.save()
        log_activity(request.user, "create", user, diff({}, snapshot(user, USER_FIELDS)))
        messages.success(
            request, f"User {user.username} added. Now give them a company and rights."
        )
        return redirect("accounts:user_detail", pk=user.pk)
    return render(request, "accounts/user_create.html", {"form": form})


def _memberships_with_departments(user):
    """Each membership gets the departments Admin can pick for its company."""
    memberships = list(user.memberships.select_related("company", "department"))
    for membership in memberships:
        membership.department_choices = Department.objects.filter(
            Q(company=membership.company, is_active=True) | Q(pk=membership.department_id)
        )
    return memberships


def _detail_context(user, **forms):
    return {
        "person": user,
        "memberships": _memberships_with_departments(user),
        "roles": Role.choices,
        "edit_form": forms.get("edit_form") or UserEditForm(instance=user),
        "reset_form": forms.get("reset_form") or PasswordResetByAdminForm(),
        "membership_form": forms.get("membership_form") or MembershipAddForm(user=user),
    }


@admin_required
def user_detail(request, pk):
    user = get_object_or_404(User, pk=pk)
    edit_form = None
    if request.method == "POST":
        before = snapshot(user, USER_FIELDS)
        edit_form = UserEditForm(request.POST, instance=user)
        if edit_form.is_valid():
            if user == request.user and not edit_form.cleaned_data["is_admin"]:
                logger.warning("%s tried to remove own Admin access", request.user.username)
                edit_form.add_error("is_admin", "You cannot remove your own Admin access.")
            else:
                saved = edit_form.save()
                changes = diff(before, snapshot(saved, USER_FIELDS))
                if changes:
                    log_activity(request.user, "update", saved, changes)
                messages.success(request, "User details saved.")
                return redirect("accounts:user_detail", pk=user.pk)
    return render(request, "accounts/user_detail.html", _detail_context(user, edit_form=edit_form))


@admin_required
@require_POST
def user_toggle(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user == request.user:
        logger.warning("%s tried to deactivate own account", request.user.username)
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("accounts:user_detail", pk=pk)
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    action = "activate" if user.is_active else "deactivate"
    log_activity(request.user, action, user, {"is_active": [not user.is_active, user.is_active]})
    messages.success(
        request, f"User {user.username} {'activated' if user.is_active else 'deactivated'}."
    )
    return redirect("accounts:user_detail", pk=pk)


@admin_required
@require_POST
def user_reset_password(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = PasswordResetByAdminForm(request.POST)
    if not form.is_valid():
        return render(request, "accounts/user_detail.html", _detail_context(user, reset_form=form))
    user.set_password(form.cleaned_data["temporary_password"])
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None
    user.save()
    log_activity(request.user, "password_reset", user)
    messages.success(
        request, f"Password reset. {user.username} must choose a new one at next login."
    )
    return redirect("accounts:user_detail", pk=pk)


# ---------- Company roles ----------


@admin_required
@require_POST
def membership_add(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = MembershipAddForm(request.POST, user=user)
    if not form.is_valid():
        return render(
            request, "accounts/user_detail.html", _detail_context(user, membership_form=form)
        )
    membership = Membership.objects.create(
        user=user, company=form.cleaned_data["company"], role=form.cleaned_data["role"]
    )
    log_activity(
        request.user,
        "create",
        membership,
        {"company": [None, membership.company.name], "role": [None, membership.role]},
    )
    messages.success(
        request,
        f"{user.username} added to {membership.company.name}. Check the ticks below and save.",
    )
    return redirect("accounts:rights_grid", pk=membership.pk)


@admin_required
@require_POST
def membership_role(request, pk):
    membership = get_object_or_404(Membership.objects.select_related("company", "user"), pk=pk)
    role = request.POST.get("role", "")
    if role not in Role.values:
        logger.warning("Invalid role %r for membership %s", role, pk)
        messages.error(request, "Please pick a valid role.")
    elif role != membership.role:
        old = membership.role
        membership.role = role
        membership.save(update_fields=["role"])
        log_activity(request.user, "update", membership, {"role": [old, role]})
        messages.success(
            request,
            f"Role changed to {membership.get_role_display()}. Existing ticks were kept.",
        )
    return redirect("accounts:user_detail", pk=membership.user_id)


@admin_required
@require_POST
def membership_department(request, pk):
    membership = get_object_or_404(
        Membership.objects.select_related("company", "user", "department"), pk=pk
    )
    wanted = request.POST.get("department", "")
    department = None
    if wanted:
        department = Department.objects.filter(
            pk=wanted if wanted.isdigit() else 0, company=membership.company, is_active=True
        ).first()
        if department is None:
            logger.warning("Invalid department %r for membership %s", wanted, pk)
            messages.error(request, "Please pick a department of this company.")
            return redirect("accounts:user_detail", pk=membership.user_id)
    if department != membership.department:
        old = membership.department
        membership.department = department
        membership.save(update_fields=["department"])
        log_activity(
            request.user,
            "update",
            membership,
            {"department": [str(old) if old else None, str(department) if department else None]},
        )
        messages.success(request, "Department saved.")
    return redirect("accounts:user_detail", pk=membership.user_id)


@admin_required
@require_POST
def membership_toggle(request, pk):
    membership = get_object_or_404(Membership.objects.select_related("company", "user"), pk=pk)
    membership.is_active = not membership.is_active
    membership.save(update_fields=["is_active"])
    action = "activate" if membership.is_active else "deactivate"
    log_activity(
        request.user,
        action,
        membership,
        {"is_active": [not membership.is_active, membership.is_active]},
    )
    return redirect("accounts:user_detail", pk=membership.user_id)


# ---------- Branch rights ----------


def _ticks_from_post(post) -> dict[int, set[str]]:
    """Checkbox names look like r_<branch id>_<right>."""
    ticks: dict[int, set[str]] = {}
    for key in post:
        prefix, _, rest = key.partition("_")
        branch_id, _, right = rest.partition("_")
        if prefix == "r" and branch_id.isdigit() and right in RIGHT_NAMES:
            ticks.setdefault(int(branch_id), set()).add(right)
    return ticks


@admin_required
def rights_grid_view(request, pk):
    membership = get_object_or_404(Membership.objects.select_related("company", "user"), pk=pk)
    user, company = membership.user, membership.company
    if request.method == "POST":
        changed = save_rights(request.user, user, company, _ticks_from_post(request.POST))
        messages.success(
            request, f"Rights saved for {changed} Branch(es)." if changed else "No changes."
        )
        return redirect("accounts:rights_grid", pk=pk)

    others = (
        User.objects.filter(memberships__company=company, memberships__is_active=True)
        .exclude(pk=user.pk)
        .order_by("username")
    )
    return render(
        request,
        "accounts/rights_grid.html",
        {
            "membership": membership,
            "person": user,
            "company": company,
            "rows": rights_grid(user, membership),
            "rights": RIGHTS,
            "copy_candidates": others,
        },
    )


@admin_required
@require_POST
def rights_copy(request, pk):
    membership = get_object_or_404(Membership.objects.select_related("company", "user"), pk=pk)
    source = User.objects.filter(
        pk=request.POST.get("source_user", "0") or 0,
        memberships__company=membership.company,
        memberships__is_active=True,
    ).first()
    if source is None or source == membership.user:
        logger.warning("Copy rights: bad source %r", request.POST.get("source_user"))
        messages.error(request, "Pick another user from this company to copy from.")
    else:
        changed = copy_rights(request.user, source, membership.user, membership.company)
        messages.success(
            request,
            f"Copied {source.username}'s rights in {membership.company.name}"
            f" ({changed} Branch(es) changed).",
        )
    return redirect("accounts:rights_grid", pk=pk)


# ---------- My profile ----------


def profile_view(request):
    """Every user can see their own details and change their own photo."""
    user = request.user
    form = ProfilePhotoForm(instance=user)
    if request.method == "POST":
        old_photo = user.photo.name
        form = ProfilePhotoForm(request.POST, request.FILES, instance=user)
        if form.is_valid() and "photo" not in form.changed_data:
            form.add_error("photo", "Choose a PNG or JPG picture first.")
        elif form.is_valid():
            form.save()
            log_activity(
                request.user, "update", user, {"photo": [old_photo or None, user.photo.name]}
            )
            if old_photo and old_photo != user.photo.name:
                _remove_photo_file(user, old_photo)
            messages.success(request, "Your photo is updated.")
            return redirect("accounts:profile")
    memberships = user.memberships.select_related("company", "department")
    context = {"form": form, "memberships": memberships}
    return render(request, "accounts/profile.html", context)


@require_POST
def profile_photo_remove(request):
    user = request.user
    old_photo = user.photo.name
    if old_photo:
        user.photo = ""
        user.save(update_fields=["photo"])
        log_activity(request.user, "update", user, {"photo": [old_photo, None]})
        _remove_photo_file(user, old_photo)
        messages.success(request, "Your photo is removed.")
    return redirect("accounts:profile")


def _remove_photo_file(user, file_name):
    """Old pictures are not records, so the file itself is cleared to save disk space."""
    try:
        user._meta.get_field("photo").storage.delete(file_name)
    except OSError as error:
        logger.warning("Could not remove old photo file %s: %s", file_name, error)
