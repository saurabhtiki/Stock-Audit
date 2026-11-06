import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from core.managers import NoDeleteQuerySet
from core.models import Branch, Company


class Item(models.Model):
    """An item of one company. Codes are unique within the company (any letter case)."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="items")
    code = models.CharField("item code", max_length=50)
    description = models.CharField(max_length=255, blank=True)
    uom = models.CharField(
        "UoM", max_length=20, blank=True, help_text="Unit of measure, e.g. PCS, KG."
    )
    is_active = models.BooleanField("active", default=True)
    is_new = models.BooleanField(
        "new", default=False, help_text="Added automatically from an ERP upload; not reviewed yet."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(Lower("code"), "company", name="unique_item_code_per_company")
        ]

    def __str__(self) -> str:
        return f"{self.code} – {self.description}" if self.description else self.code

    def delete(self, *args, **kwargs):
        raise ValidationError("Items are never deleted. Deactivate it instead.")


class Audit(models.Model):
    class Type(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        SURPRISE = "surprise", "Surprise"

    class Stage(models.TextChoices):
        ERP_DRAFT = "erp_draft", "ERP Draft"
        ERP_FROZEN = "erp_frozen", "ERP Frozen"
        CHECKING_DONE = "checking_done", "Checking Done"
        LOCKED = "locked", "Locked"
        CANCELLED = "cancelled", "Cancelled"

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="audits")
    title = models.CharField(
        max_length=60, help_text="Short name, unique within the Branch, e.g. “Aug26”."
    )
    audit_date = models.DateField(help_text="Day the physical count happens.")
    audit_type = models.CharField(
        "type",
        max_length=20,
        choices=Type.choices,
        default=Type.SCHEDULED,
        help_text="Scheduled = planned audit. Surprise = unannounced check, reported separately.",
    )
    stock_as_on = models.DateField(
        "ERP stock as on",
        help_text="Date of the ERP stock report. Must be on or before the audit date.",
    )
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.ERP_DRAFT)

    qty_tolerance = models.DecimalField(
        "quantity tolerance", max_digits=14, decimal_places=3, default=Decimal(0)
    )
    pct_tolerance = models.DecimalField(
        "percentage tolerance", max_digits=6, decimal_places=2, null=True, blank=True
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    frozen_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    frozen_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)
    checking_done_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    checking_done_at = models.DateTimeField(null=True, blank=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["-audit_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                Lower("title"),
                "branch",
                condition=~Q(stage="cancelled"),
                name="unique_audit_title_per_branch",
            )
        ]

    def __str__(self) -> str:
        return f"{self.branch.code} – {self.title}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Audits are never deleted. Cancel it instead.")

    @property
    def is_draft(self) -> bool:
        return self.stage == self.Stage.ERP_DRAFT

    @property
    def is_counting(self) -> bool:
        """Counts can be entered or changed (ERP Frozen, before Checking Done)."""
        return self.stage == self.Stage.ERP_FROZEN

    @property
    def remarks_open(self) -> bool:
        """Remarks can be added or changed (after freezing, until Locked)."""
        return self.stage in (self.Stage.ERP_FROZEN, self.Stage.CHECKING_DONE)

    @property
    def current_upload(self):
        return self.uploads.filter(status=ErpUpload.Status.USED).first()


def erp_upload_path(instance, filename: str) -> str:
    # Only used by migration 0001; ERP files are no longer stored (Phase 2.1).
    return f"erp/{instance.audit_id}/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


class ErpUpload(models.Model):
    """One checked ERP file of an audit. The file itself is never stored.

    A file without errors keeps its checked lines in `rows` until the user confirms;
    after confirming they live in AuditLine and `rows` is emptied.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Not confirmed"
        USED = "used", "In use"
        REPLACED = "replaced", "Replaced"

    audit = models.ForeignKey(Audit, on_delete=models.PROTECT, related_name="uploads")
    rows = models.JSONField(default=list, blank=True)
    original_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    line_count = models.PositiveIntegerField(default=0)
    total_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal(0))

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        verbose_name = "ERP upload"

    def __str__(self) -> str:
        return f"{self.audit} – {self.original_name}"


class AuditLine(models.Model):
    """One ERP stock line of an audit. Description, UoM and rate are copied from the file.

    After freezing, the auditor fills `physical_qty`; variance and status are worked out
    from it (see audits.counting) and stored so the table can filter and sort on them.
    """

    class Status(models.TextChoices):
        NOT_COUNTED = "not_counted", "Not counted"
        OK = "ok", "OK"
        EXCESS = "excess", "Excess"
        SHORTAGE = "shortage", "Shortage"

    audit = models.ForeignKey(Audit, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="audit_lines")
    item_code = models.CharField(max_length=50)
    description = models.CharField(max_length=255, blank=True)
    uom = models.CharField("UoM", max_length=20, blank=True)
    erp_qty = models.DecimalField("ERP qty", max_digits=14, decimal_places=3)
    rate = models.DecimalField("rate p.u.", max_digits=14, decimal_places=2)
    erp_value = models.DecimalField("ERP value", max_digits=18, decimal_places=2)

    physical_qty = models.DecimalField(
        "physical qty", max_digits=14, decimal_places=3, null=True, blank=True
    )
    variance_qty = models.DecimalField(
        "variance qty", max_digits=15, decimal_places=3, null=True, blank=True
    )
    variance_value = models.DecimalField(
        "variance value", max_digits=19, decimal_places=2, null=True, blank=True
    )
    count_status = models.CharField(
        "status", max_length=20, choices=Status.choices, default=Status.NOT_COUNTED, db_index=True
    )
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    counted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["item_code"]
        verbose_name = "audit line"
        constraints = [
            models.UniqueConstraint(fields=["audit", "item"], name="unique_item_per_audit")
        ]

    def __str__(self) -> str:
        return f"{self.audit} – {self.item_code}"


class Remark(models.Model):
    """A note on one audit line, or on the whole audit (line empty). Never really deleted."""

    audit = models.ForeignKey(Audit, on_delete=models.PROTECT, related_name="remarks")
    line = models.ForeignKey(
        AuditLine, null=True, blank=True, on_delete=models.PROTECT, related_name="remarks"
    )
    text = models.TextField(max_length=1000)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        where = self.line.item_code if self.line_id else "general"
        return f"{self.audit} – remark on {where}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Remarks are never deleted. Mark them deleted instead.")


class CountUpload(models.Model):
    """One checked count sheet. The file is never stored; checked rows wait here until Confirm."""

    class Status(models.TextChoices):
        PENDING = "pending", "Not confirmed"
        USED = "used", "Saved"

    audit = models.ForeignKey(Audit, on_delete=models.PROTECT, related_name="count_uploads")
    rows = models.JSONField(default=list, blank=True)
    original_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        verbose_name = "count sheet upload"

    def __str__(self) -> str:
        return f"{self.audit} – {self.original_name}"


class Query(models.Model):
    """Raised at Checking Done for each Excess / Shortage line.

    Open → Reply Submitted → Action Pending → Closed (see audits.queries).
    A query that is not Closed is carried into the Branch's later audits (same record).
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        REPLY_SUBMITTED = "reply_submitted", "Reply Submitted"
        ACTION_PENDING = "action_pending", "Action Pending"
        CLOSED = "closed", "Closed"

    audit = models.ForeignKey(Audit, on_delete=models.PROTECT, related_name="queries")
    line = models.OneToOneField(AuditLine, on_delete=models.PROTECT, related_name="query")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="queries")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    close_note = models.TextField(blank=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["line__item_code"]
        verbose_name_plural = "queries"

    def __str__(self) -> str:
        return f"Query on {self.line}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Queries are never deleted.")

    @property
    def is_closed(self) -> bool:
        return self.status == self.Status.CLOSED


class QueryReply(models.Model):
    """The one reply to a query. Later news goes into QueryUpdate rows."""

    query = models.OneToOneField(Query, on_delete=models.PROTECT, related_name="reply")
    text = models.TextField(max_length=2000)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    objects = NoDeleteQuerySet.as_manager()

    def __str__(self) -> str:
        return f"Reply to {self.query}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Replies are never deleted.")


class QueryUpdate(models.Model):
    """A dated note added under the reply, e.g. “Transfer slip raised today.”"""

    query = models.ForeignKey(Query, on_delete=models.PROTECT, related_name="updates")
    text = models.TextField(max_length=2000)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"Update on {self.query}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Updates are never deleted.")


class Action(models.Model):
    """Something to do to settle a query: Open → Done → Verified."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DONE = "done", "Done"
        VERIFIED = "verified", "Verified"

    query = models.ForeignKey(Query, on_delete=models.PROTECT, related_name="actions")
    description = models.TextField(max_length=1000)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    done_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    done_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"Action on {self.query}: {self.description[:40]}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Actions are never deleted.")

    @property
    def is_overdue(self) -> bool:
        return (
            self.due_date is not None
            and self.status != self.Status.VERIFIED
            and self.due_date < timezone.localdate()
        )
