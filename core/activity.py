"""Helpers to write the activity log."""

import logging

from django.db import models
from django.db.models.fields.files import FieldFile

from core.models import ActivityLog

logger = logging.getLogger(__name__)


def _plain(value):
    """Make a value safe to store as JSON (files → name, objects → text)."""
    if isinstance(value, models.Model):
        return str(value)
    if isinstance(value, FieldFile):
        return value.name or ""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def snapshot(instance: models.Model, fields: list[str]) -> dict:
    return {name: _plain(getattr(instance, name)) for name in fields}


def diff(before: dict, after: dict) -> dict:
    """{field: [old, new]} for fields that changed."""
    return {
        name: [before.get(name), after.get(name)]
        for name in after
        if before.get(name) != after.get(name)
    }


def _entry(actor, action: str, record: models.Model, changes: dict | None) -> ActivityLog:
    return ActivityLog(
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        record_type=record._meta.verbose_name.title(),
        record_id=str(record.pk),
        record_label=str(record)[:200],
        changes=changes or {},
    )


def log_activity(actor, action: str, record: models.Model, changes: dict | None = None):
    entry = _entry(actor, action, record, changes)
    entry.save()
    logger.info("activity: %s %s %s by %s", action, entry.record_type, record.pk, actor)
    return entry


def log_many(actor, action: str, records_and_changes) -> int:
    """One log row per (record, changes) pair, written in one go (e.g. a count sheet)."""
    entries = [_entry(actor, action, record, changes) for record, changes in records_and_changes]
    ActivityLog.objects.bulk_create(entries, batch_size=2000)
    logger.info("activity: %s × %s by %s", action, len(entries), actor)
    return len(entries)
