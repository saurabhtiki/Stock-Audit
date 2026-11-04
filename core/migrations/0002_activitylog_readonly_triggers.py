"""Database-level guard: activity log rows can never be changed or deleted."""

from django.db import migrations

TABLE = "core_activitylog"

CREATE = [
    f"""
    CREATE TRIGGER activitylog_no_update BEFORE UPDATE ON {TABLE}
    BEGIN SELECT RAISE(ABORT, 'The activity log cannot be changed.'); END;
    """,
    f"""
    CREATE TRIGGER activitylog_no_delete BEFORE DELETE ON {TABLE}
    BEGIN SELECT RAISE(ABORT, 'The activity log cannot be deleted.'); END;
    """,
]
DROP = [
    "DROP TRIGGER IF EXISTS activitylog_no_update;",
    "DROP TRIGGER IF EXISTS activitylog_no_delete;",
]


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunSQL(sql=CREATE, reverse_sql=DROP)]
