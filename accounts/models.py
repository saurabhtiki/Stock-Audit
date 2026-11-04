from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone

from accounts.rights import RIGHT_NAMES, RIGHTS, Role
from core.managers import NoDeleteUserManager
from core.models import Branch, Company, Department


def validate_photo_size(file) -> None:
    limit = settings.USER_PHOTO_MAX_BYTES
    if file.size > limit:
        raise ValidationError(f"Photo is too big. Maximum size is {limit // (1024 * 1024)} MB.")


class User(AbstractUser):
    """One login for the whole group. Username and email are unique across all companies."""

    first_name = None
    last_name = None
    full_name = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    is_admin = models.BooleanField(
        "Admin", default=False, help_text="Group-wide Admin: manages all companies and users."
    )
    photo = models.ImageField(
        upload_to="users/",
        blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg"]), validate_photo_size],
        help_text="PNG or JPG. It is shrunk automatically.",
    )
    must_change_password = models.BooleanField(default=True)
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    REQUIRED_FIELDS = ["email", "full_name"]
    objects = NoDeleteUserManager()

    class Meta:
        ordering = ["username"]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.username})"

    def save(self, *args, **kwargs):
        self.username = self.username.strip().lower()
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Users are never deleted. Deactivate the user instead.")

    @property
    def is_login_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > timezone.now()


class Membership(models.Model):
    """A user's access to one company, with one role in that company."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="memberships")
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices)
    department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="memberships",
        help_text="Set by Admin from the company's department list.",
    )
    is_active = models.BooleanField("active", default=True)

    class Meta:
        ordering = ["company__name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "company"], name="unique_membership")
        ]

    def __str__(self) -> str:
        return f"{self.user.username} @ {self.company.name} ({self.get_role_display()})"


class BranchRight(models.Model):
    """Which of the 9 rights a user has in one Branch."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="branch_rights")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="user_rights")

    upload_erp = models.BooleanField(default=False)
    set_tolerance_freeze = models.BooleanField(default=False)
    count_and_remarks = models.BooleanField(default=False)
    upload_media = models.BooleanField(default=False)
    reply_queries = models.BooleanField(default=False)
    manage_actions = models.BooleanField(default=False)
    verify_close_queries = models.BooleanField(default=False)
    lock_audit = models.BooleanField(default=False)
    view_reports = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "branch"], name="unique_branch_right")
        ]

    def __str__(self) -> str:
        return f"{self.user.username} @ {self.branch}"

    def granted(self) -> set[str]:
        return {name for name in RIGHT_NAMES if getattr(self, name)}


# Keep the field list and the RIGHTS table in step.
assert {f.name for f in BranchRight._meta.fields} >= {name for name, *_ in RIGHTS}
