"""Lock an audit (from Checking Done) and reopen it (Admin only, reason required)."""

import logging

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone

from audits import counting
from audits.models import Attachment, Audit, Query
from audits.permissions import can_lock, can_reopen_audit, forbidden, get_visible_audit
from core.activity import log_activity

logger = logging.getLogger(__name__)


class LockForm(forms.Form):
    confirm = forms.BooleanField(
        label=(
            "I understand remarks, media and Closed queries are frozen after this. "
            "Only Admin can reopen the audit."
        ),
        error_messages={"required": "Tick the box to confirm the lock."},
    )


class ReopenForm(forms.Form):
    reason = forms.CharField(
        min_length=5,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "textarea w-full"}),
        help_text="Why the audit is reopened, e.g. “Count sheet was wrong for 3 lines”. Logged.",
        error_messages={
            "required": "A reason is required to reopen an audit.",
            "min_length": "Write a reason of at least 5 characters.",
        },
    )


def lock_summary(audit: Audit) -> dict:
    summary = counting.count_summary(audit)
    summary.update(
        Query.objects.filter(audit=audit).aggregate(
            query_total=Count("id"),
            query_closed=Count("id", filter=Q(status=Query.Status.CLOSED)),
        )
    )
    summary["query_open"] = summary["query_total"] - summary["query_closed"]
    summary.update(
        Attachment.objects.filter(audit=audit, is_deleted=False).aggregate(
            photos=Count("id", filter=Q(kind=Attachment.Kind.PHOTO)),
            other_media=Count("id", filter=~Q(kind=Attachment.Kind.PHOTO)),
        )
    )
    return summary


def audit_lock(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_lock(request.user, audit):
        return forbidden(
            request,
            "You need the “Lock” right in this Branch, and the audit must be at Checking Done.",
        )
    form = LockForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked = Audit.objects.select_for_update().select_related("branch").get(pk=audit.pk)
            if not can_lock(request.user, locked):
                logger.info("Lock of audit %s refused: stage is %s", audit.pk, locked.stage)
                messages.error(request, "This audit can no longer be locked (its stage changed).")
                return redirect("audits:audit_detail", pk=audit.pk)
            locked.stage = Audit.Stage.LOCKED
            locked.locked_by, locked.locked_at = request.user, timezone.now()
            locked.save(update_fields=["stage", "locked_by", "locked_at"])
            log_activity(
                request.user, "lock", locked, {"stage": [Audit.Stage.CHECKING_DONE, locked.stage]}
            )
        messages.success(
            request, f"Audit “{audit.title}” locked. Open queries still carry forward."
        )
        return redirect("audits:audit_detail", pk=audit.pk)
    if form.is_bound:
        logger.info("Lock of audit %s not confirmed: %s", audit.pk, form.errors.as_text())
    return render(
        request,
        "audits/audit_lock.html",
        {"audit": audit, "form": form, "summary": lock_summary(audit)},
    )


def audit_reopen(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_reopen_audit(request.user, audit):
        return forbidden(request, "Only Admin can reopen an audit, and only when it is Locked.")
    form = ReopenForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        reason = form.cleaned_data["reason"].strip()
        with transaction.atomic():
            reopened = Audit.objects.select_for_update().select_related("branch").get(pk=audit.pk)
            if not can_reopen_audit(request.user, reopened):
                logger.info("Reopen of audit %s refused: stage is %s", audit.pk, reopened.stage)
                messages.error(request, "This audit is no longer Locked.")
                return redirect("audits:audit_detail", pk=audit.pk)
            reopened.stage = Audit.Stage.CHECKING_DONE
            reopened.locked_by, reopened.locked_at = None, None
            reopened.save(update_fields=["stage", "locked_by", "locked_at"])
            log_activity(
                request.user,
                "reopen",
                reopened,
                {"stage": [Audit.Stage.LOCKED, reopened.stage], "reason": [None, reason]},
            )
        messages.success(
            request,
            f"Audit “{audit.title}” reopened. It is back at Checking Done; counts stay fixed.",
        )
        return redirect("audits:audit_detail", pk=audit.pk)
    if form.is_bound:
        logger.info("Reopen of audit %s refused: %s", audit.pk, form.errors.as_text())
    return render(request, "audits/audit_reopen.html", {"audit": audit, "form": form})
