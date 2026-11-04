import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

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
    def current_upload(self):
        return self.uploads.filter(status=ErpUpload.Status.USED).first()


def erp_upload_path(instance, filename: str) -> str:
    # Random name on disk; the original name is kept in the database.
    return f"erp/{instance.audit_id}/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


class ErpUpload(models.Model):
    """Every ERP file uploaded for an audit, kept on disk for traceability."""

    class Status(models.TextChoices):
        PENDING = "pending", "Not confirmed"
        USED = "used", "In use"
        REPLACED = "replaced", "Replaced"

    audit = models.ForeignKey(Audit, on_delete=models.PROTECT, related_name="uploads")
    file = models.FileField(upload_to=erp_upload_path)
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
    """One ERP stock line of an audit. Description, UoM and rate are copied from the file."""

    audit = models.ForeignKey(Audit, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="audit_lines")
    item_code = models.CharField(max_length=50)
    description = models.CharField(max_length=255, blank=True)
    uom = models.CharField("UoM", max_length=20, blank=True)
    erp_qty = models.DecimalField("ERP qty", max_digits=14, decimal_places=3)
    rate = models.DecimalField("rate p.u.", max_digits=14, decimal_places=2)
    erp_value = models.DecimalField("ERP value", max_digits=18, decimal_places=2)

    class Meta:
        ordering = ["item_code"]
        constraints = [
            models.UniqueConstraint(fields=["audit", "item"], name="unique_item_per_audit")
        ]

    def __str__(self) -> str:
        return f"{self.audit} – {self.item_code}"
