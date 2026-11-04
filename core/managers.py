"""Querysets that refuse bulk delete, so 'never deleted' holds everywhere."""

from django.contrib.auth.models import UserManager
from django.core.exceptions import ValidationError
from django.db import models


class NoDeleteQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError(
            f"{self.model._meta.verbose_name_plural.title()} are never deleted. Deactivate instead."
        )


class NoDeleteUserManager(UserManager.from_queryset(NoDeleteQuerySet)):
    pass
