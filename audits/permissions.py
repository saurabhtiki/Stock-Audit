"""Who may see or change audits and items."""

import logging

from django.http import Http404
from django.shortcuts import get_object_or_404, render

from accounts.access import has_right, visible_branches
from accounts.models import BranchRight, Membership
from audits.models import Audit
from core.models import Company

logger = logging.getLogger(__name__)


def forbidden(request, reason: str):
    logger.warning("%s refused at %s: %s", request.user.username, request.path, reason)
    return render(request, "core/forbidden.html", {"reason": reason}, status=403)


def get_visible_audit(request, pk: int) -> Audit:
    """The audit, if the user has any right in its Branch; otherwise 'not found'."""
    audit = get_object_or_404(
        Audit.objects.select_related("branch__company", "created_by", "frozen_by"), pk=pk
    )
    if not visible_branches(request.user, audit.branch.company).filter(pk=audit.branch_id).exists():
        logger.warning("%s tried to open audit %s", request.user.username, pk)
        raise Http404("Audit not found.")
    return audit


def branches_with_right(user, company: Company | None, right: str):
    return [b for b in visible_branches(user, company) if has_right(user, b, right)]


def can_manage_items(user, company: Company | None) -> bool:
    """Admin, or anyone with 'set tolerance & freeze' in any Branch of the company."""
    if company is None or not company.is_active:
        return False
    if user.is_admin:
        return True
    if not Membership.objects.filter(user=user, company=company, is_active=True).exists():
        return False
    return BranchRight.objects.filter(
        user=user, branch__company=company, branch__is_active=True, set_tolerance_freeze=True
    ).exists()


def can_edit_audit(user, audit: Audit) -> bool:
    """While ERP Draft: the creator (still with upload right) or Admin."""
    if not audit.is_draft or not audit.branch.is_usable:
        return False
    if user.is_admin:
        return True
    return audit.created_by_id == user.pk and has_right(user, audit.branch, "upload_erp")


def can_cancel_audit(user, audit: Audit) -> bool:
    """Admin at Draft or Frozen (nothing counted yet); creator only their own Draft."""
    if user.is_admin:
        return audit.stage in (Audit.Stage.ERP_DRAFT, Audit.Stage.ERP_FROZEN)
    return can_edit_audit(user, audit)


def can_upload(user, audit: Audit) -> bool:
    return audit.is_draft and has_right(user, audit.branch, "upload_erp")


def can_freeze(user, audit: Audit) -> bool:
    return audit.is_draft and has_right(user, audit.branch, "set_tolerance_freeze")
