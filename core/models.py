from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models.functions import Lower

from core.managers import NoDeleteQuerySet


def validate_logo_size(file) -> None:
    limit = settings.COMPANY_LOGO_MAX_BYTES
    if file.size > limit:
        raise ValidationError(f"Logo is too big. Maximum size is {limit // (1024 * 1024)} MB.")


class Company(models.Model):
    name = models.CharField(max_length=150, unique=True)
    address = models.TextField(blank=True)
    logo = models.ImageField(
        upload_to="logos/",
        blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg"]), validate_logo_size],
        help_text="PNG or JPG, up to 1 MB.",
    )
    is_active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "companies"

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise ValidationError("Companies are never deleted. Deactivate it instead.")


class Branch(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="branches")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30, help_text="Short code, unique within the company.")
    is_active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["company__name", "code"]
        verbose_name_plural = "branches"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="unique_branch_code_per_company"
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} – {self.name}"

    @property
    def is_usable(self) -> bool:
        """A Branch works only if it and its company are both active."""
        return self.is_active and self.company.is_active

    def delete(self, *args, **kwargs):
        raise ValidationError("Branches are never deleted. Deactivate it instead.")


class Department(models.Model):
    """A department of one company, e.g. Stores or Finance. Users are picked from this list."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="departments")
    name = models.CharField(max_length=100)
    is_active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["company__name", "name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), "company", name="unique_department_per_company")
        ]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise ValidationError("Departments are never deleted. Deactivate it instead.")


class ActivityLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("The activity log cannot be changed.")

    def delete(self):
        raise ValidationError("The activity log cannot be deleted.")


class ActivityLog(models.Model):
    """Every change: who, when, which record, old value → new value.

    Write-only: rows can be added, never changed or removed (also blocked by
    database triggers in the migration).
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    action = models.CharField(max_length=40)
    record_type = models.CharField(max_length=40, db_index=True)
    record_id = models.CharField(max_length=40)
    record_label = models.CharField(max_length=200)
    changes = models.JSONField(default=dict, blank=True)

    objects = ActivityLogQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return (
            f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.record_type} {self.record_label}"
        )

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("The activity log cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("The activity log cannot be deleted.")


class BackupLog(models.Model):
    """One backup zip downloaded by Admin. Used for “Last backup” and the 7-day reminder."""

    class Kind(models.TextChoices):
        DATABASE = "database", "Database only"
        FULL = "full", "Database + media"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    size = models.PositiveBigIntegerField(help_text="Size of the zip in bytes.")
    file_name = models.CharField(max_length=100)

    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "backup"

    def __str__(self) -> str:
        return f"{self.file_name} ({self.get_kind_display()})"

    def delete(self, *args, **kwargs):
        raise ValidationError("Backup records are never deleted.")
