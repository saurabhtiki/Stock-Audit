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
    """Admin at Draft, or at Frozen while nothing is counted; creator only their own Draft."""
    if user.is_admin:
        if audit.stage == Audit.Stage.ERP_FROZEN:
            return not has_counts(audit)
        return audit.is_draft
    return can_edit_audit(user, audit)


def has_counts(audit: Audit) -> bool:
    return audit.lines.filter(physical_qty__isnull=False).exists()


def can_upload(user, audit: Audit) -> bool:
    return audit.is_draft and has_right(user, audit.branch, "upload_erp")


def can_freeze(user, audit: Audit) -> bool:
    return audit.is_draft and has_right(user, audit.branch, "set_tolerance_freeze")


def can_count(user, audit: Audit) -> bool:
    """Enter counts, upload the count sheet, mark Checking Done: ERP Frozen + Count right."""
    return audit.is_counting and has_right(user, audit.branch, "count_and_remarks")


def can_remark(user, audit: Audit) -> bool:
    """Add remarks: from freezing until Locked, with the Count right."""
    return audit.remarks_open and has_right(user, audit.branch, "count_and_remarks")


QUERY_STAGES = (Audit.Stage.CHECKING_DONE, Audit.Stage.LOCKED)
# Stages with counts: the count table, media tab and PDF are available.
COUNT_STAGES = (Audit.Stage.ERP_FROZEN, *QUERY_STAGES)


def queries_open(audit: Audit) -> bool:
    """Queries can be worked on from Checking Done. Locking freezes only Closed queries;
    open ones stay live and carry forward."""
    return audit.stage in QUERY_STAGES and audit.branch.is_usable


def can_reply(user, query) -> bool:
    return (
        not query.is_closed
        and queries_open(query.audit)
        and has_right(user, query.audit.branch, "reply_queries")
    )


def can_edit_reply(user, query) -> bool:
    reply = getattr(query, "reply", None)
    return reply is not None and reply.created_by_id == user.pk and can_reply(user, query)


def can_add_action(user, query) -> bool:
    return (
        query.status in (query.Status.REPLY_SUBMITTED, query.Status.ACTION_PENDING)
        and queries_open(query.audit)
        and has_right(user, query.audit.branch, "manage_actions")
    )


def can_mark_done(user, action) -> bool:
    """The owner, or anyone with the Actions right, while the query is not Closed."""
    query = action.query
    if query.is_closed or not queries_open(query.audit):
        return False
    return action.owner_id == user.pk or has_right(user, query.audit.branch, "manage_actions")


def can_verify_close(user, query) -> bool:
    return queries_open(query.audit) and has_right(user, query.audit.branch, "verify_close_queries")


def can_reopen_query(user, query) -> bool:
    """A Closed query of a Locked audit stays frozen until Admin reopens the audit."""
    return query.is_closed and not query.audit.is_locked and can_verify_close(user, query)


# ---------- Lock / reopen ----------


def can_lock(user, audit: Audit) -> bool:
    return audit.stage == Audit.Stage.CHECKING_DONE and has_right(user, audit.branch, "lock_audit")


def can_reopen_audit(user, audit: Audit) -> bool:
    return user.is_admin and audit.is_locked and audit.branch.is_usable


# ---------- Media ----------

MEDIA_RIGHTS = ("upload_media", "reply_queries", "manage_actions")


def media_open(audit: Audit, query=None) -> bool:
    """Media can change: audit / line media from ERP Frozen until Locked; query / action
    media while the query is not Closed (open queries stay live after lock)."""
    if not audit.branch.is_usable:
        return False
    if query is not None:
        return queries_open(query.audit) and not query.is_closed
    return audit.remarks_open


def can_add_media(user, audit: Audit, query=None) -> bool:
    """Audit / line: “Media” right. Query / action: also the Reply or Actions right."""
    if not media_open(audit, query):
        return False
    rights = MEDIA_RIGHTS if query is not None else ("upload_media",)
    return any(has_right(user, audit.branch, right) for right in rights)


def can_remove_media(user, attachment) -> bool:
    """The uploader or Admin, while the media is still open."""
    if attachment.is_deleted or not media_open(attachment.audit, attachment.query):
        return False
    return user.is_admin or (
        attachment.uploaded_by_id == user.pk
        and can_add_media(user, attachment.audit, attachment.query)
    )


def can_tick_report(user, attachment) -> bool:
    """Choose photos for the PDF: frozen once the audit is Locked, so a signed PDF stays the same."""
    return not attachment.audit.is_locked and can_add_media(
        user, attachment.audit, attachment.query
    )


def can_view_reports(user, audit: Audit) -> bool:
    return audit.stage != Audit.Stage.CANCELLED and has_right(user, audit.branch, "view_reports")


def can_download_pdf(user, audit: Audit) -> bool:
    return audit.stage in COUNT_STAGES and can_view_reports(user, audit)


def can_change_remark(user, remark) -> bool:
    """Only the writer edits or deletes, and only until the audit is Locked."""
    return (
        not remark.is_deleted and remark.created_by_id == user.pk and can_remark(user, remark.audit)
    )
