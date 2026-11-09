"""Phase 6: dashboards (status grid, ageing, overdue, variance, repeat items, trend) and backup."""

import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import QueryDict
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.rights import Role
from audits import dashboards
from audits.models import Action, Audit, AuditLine, Item, Query
from core import backup
from core.models import ActivityLog, BackupLog, Branch, Company

from .conftest import client_for, make_user
from .test_audits import grant

S = AuditLine.Status
TODAY = date(2026, 11, 6)


# ---------- helpers ----------


def make_audit(
    branch, title, day, owner, stage=Audit.Stage.CHECKING_DONE, lines=(), kind="scheduled"
):
    """lines: (item code, erp qty, physical qty or None, rate). Status: any variance = flagged
    when over 1 unit, so a 1-unit difference stays OK (inside tolerance)."""
    audit = Audit.objects.create(
        branch=branch,
        title=title,
        audit_date=day,
        stock_as_on=day,
        stage=stage,
        audit_type=kind,
        created_by=owner,
        qty_tolerance=Decimal(1),
    )
    for code, erp, physical, rate in lines:
        item, _ = Item.objects.get_or_create(company=branch.company, code=code)
        erp, rate = Decimal(erp), Decimal(rate)
        line = AuditLine(
            audit=audit, item=item, item_code=code, erp_qty=erp, rate=rate, erp_value=erp * rate
        )
        if physical is not None:
            line.physical_qty = Decimal(physical)
            line.variance_qty = line.physical_qty - erp
            line.variance_value = line.variance_qty * rate
            if abs(line.variance_qty) <= 1:
                line.count_status = S.OK
            else:
                line.count_status = S.EXCESS if line.variance_qty > 0 else S.SHORTAGE
        line.save()
        if line.count_status in (S.EXCESS, S.SHORTAGE) and stage in (
            Audit.Stage.CHECKING_DONE,
            Audit.Stage.LOCKED,
        ):
            Query.objects.create(audit=audit, line=line, item=item, created_by=owner)
    return audit


def filters_for(branches, today=TODAY, **params):
    query = QueryDict(mutable=True)
    for key, value in params.items():
        query.setlist(key, value if isinstance(value, list) else [value])
    return dashboards.read_filters(query, branches, today=today)


@pytest.fixture
def north(branches):
    return branches[0]


@pytest.fixture
def south(branches):
    return branches[1]


@pytest.fixture
def viewer(branches, company_a):
    """View reports in North only."""
    user = make_user("vani")
    grant(user, branches[0], company_a, role=Role.VIEWER, view_reports=True)
    return user


def dash(client, tab="status", **params):
    return client.get(reverse("audits:dashboard_tab", args=[tab]), params)


# ---------- filters ----------


def test_default_filters_last_12_months(branches):
    filters = filters_for(branches)
    assert filters.month_from == date(2025, 12, 1)
    assert filters.date_to == date(2026, 11, 30)
    assert filters.branches == list(branches)


def test_bad_filters_fall_back_with_message(branches, north):
    filters = filters_for(
        branches, branch=[str(north.pk), "99999"], **{"from": "abc", "to": "2026-01"}
    )
    assert filters.branches == [north]
    assert filters.month_from == date(2025, 12, 1)
    assert len(filters.errors) == 2


# ---------- rights ----------


def test_dashboard_needs_view_reports(branches, company_a):
    user = make_user("cora")
    grant(user, branches[0], company_a, role=Role.AUDITOR, count_and_remarks=True)
    assert dash(client_for(user)).status_code == 403


def test_viewer_sees_only_own_branch(viewer, north, south, admin_user):
    make_audit(north, "N-Aug", TODAY, admin_user)
    make_audit(south, "S-Aug", TODAY, admin_user)
    body = dash(client_for(viewer)).content.decode()
    assert "N-Aug" in body and "S-Aug" not in body
    # Even asking for the other Branch by URL shows nothing of it.
    body = dash(client_for(viewer), branch=str(south.pk)).content.decode()
    assert "S-Aug" not in body


def test_other_company_never_shows(admin_client, admin_user, north, company_b):
    other = Branch.objects.create(company=company_b, name="Far", code="F1")
    make_audit(other, "Far-Aug", TODAY, admin_user)
    make_audit(north, "N-Aug", TODAY, admin_user)
    session = admin_client.session
    session["company_id"] = north.company_id
    session.save()
    body = dash(admin_client).content.decode()
    assert "Far-Aug" not in body


def test_unknown_tab_404(admin_client, branches):
    assert dash(admin_client, "nope").status_code == 404


# ---------- status grid ----------


def test_status_grid_counts(admin_user, branches, north):
    audit = make_audit(
        north,
        "Aug26",
        TODAY,
        admin_user,
        lines=[("A1", 10, 5, 2), ("A2", 10, 10, 2), ("A3", 10, None, 2), ("A4", 4, 4, 1)],
    )
    Query.objects.filter(line__item_code="A1").update(status=Query.Status.OPEN)
    make_audit(north, "Gone", TODAY, admin_user, stage=Audit.Stage.CANCELLED)
    rows = list(dashboards.status_rows(filters_for(branches)))
    assert [row.title for row in rows] == ["Aug26"]
    row = rows[0]
    assert (row.line_count, row.counted_count, row.pct_counted, row.open_queries) == (4, 3, 75, 1)
    assert audit.pk == row.pk
    with_cancelled = dashboards.status_rows(filters_for(branches), include_cancelled=True)
    assert {row.title for row in with_cancelled} == {"Aug26", "Gone"}


def test_status_page_and_csv(admin_client, admin_user, north, branches):
    make_audit(north, "Aug26", TODAY, admin_user, lines=[("A1", 10, 5, 2)])
    make_audit(north, "Old", date(2024, 1, 5), admin_user)
    response = dash(admin_client)
    body = response.content.decode()
    assert response.status_code == 200 and "Aug26" in body and "Old" not in body
    csv_text = b"".join(dash(admin_client, export="csv").streaming_content).decode("utf-8-sig")
    assert "Aug26" in csv_text and "Old" not in csv_text


def test_filters_type_and_months(admin_user, branches, north):
    make_audit(north, "Sched", date(2026, 5, 1), admin_user)
    make_audit(north, "Surp", date(2026, 5, 2), admin_user, kind="surprise")
    make_audit(north, "June", date(2026, 6, 1), admin_user)
    filters = filters_for(branches, type="surprise")
    assert [a.title for a in dashboards.status_rows(filters)] == ["Surp"]
    filters = filters_for(branches, **{"from": "2026-06", "to": "2026-06"})
    assert [a.title for a in dashboards.status_rows(filters)] == ["June"]


# ---------- queries & actions ----------


def test_ageing_bucket_edges(admin_user, branches, north):
    audit = make_audit(
        north,
        "Aug26",
        TODAY,
        admin_user,
        lines=[(f"Q{i}", 10, 0, 1) for i in range(7)],
    )
    ages = [0, 7, 8, 30, 31, 90, 91]
    for query, age in zip(audit.queries.order_by("line__item_code"), ages, strict=True):
        created = timezone.make_aware(
            timezone.datetime.combine(TODAY - timedelta(days=age), timezone.datetime.min.time())
        ) + timedelta(hours=12)
        Query.objects.filter(pk=query.pk).update(created_at=created)
    Query.objects.filter(line__item_code="Q6").update(status=Query.Status.CLOSED)  # 91 days, closed
    rows = dashboards.ageing_rows(filters_for(branches), today=TODAY)
    north_row = next(row for row in rows if row["branch"] == "N1")
    #               0–7  8–30  31–60  61–90  90+
    assert north_row["buckets"] == [2, 2, 1, 1, 0]
    assert north_row["total"] == 6


def test_overdue_actions(admin_user, branches, north):
    audit = make_audit(north, "Aug26", TODAY, admin_user, lines=[("A1", 10, 0, 1)])
    query = audit.queries.get()
    common = {"query": query, "owner": admin_user, "created_by": admin_user}
    Action.objects.create(description="late", due_date=TODAY - timedelta(days=3), **common)
    Action.objects.create(description="no due", **common)
    Action.objects.create(description="future", due_date=TODAY + timedelta(days=1), **common)
    Action.objects.create(
        description="verified",
        due_date=TODAY - timedelta(days=9),
        status=Action.Status.VERIFIED,
        **common,
    )
    Action.objects.create(
        description="done", due_date=TODAY - timedelta(days=1), status=Action.Status.DONE, **common
    )
    rows = dashboards.overdue_actions(filters_for(branches), today=TODAY)
    # Done but not Verified stays overdue (it waits for someone to verify it).
    assert [(row.description, row.days_late.days, row.status) for row in rows] == [
        ("late", 3, "open"),
        ("done", 1, "done"),
    ]
    assert rows[0].branch_code == "N1" and rows[0].item_code == "A1"


def test_queries_tab_page_and_csv(admin_client, admin_user, north):
    make_audit(north, "Aug26", timezone.localdate(), admin_user, lines=[("A1", 10, 0, 1)])
    assert dash(admin_client, "queries").status_code == 200
    csv_text = b"".join(dash(admin_client, "queries", export="csv").streaming_content).decode(
        "utf-8-sig"
    )
    assert csv_text.splitlines()[1].startswith("N1,1,")


# ---------- variance ----------


def test_variance_includes_within_tolerance_excludes_not_counted_and_open_audits(
    admin_user, branches, north
):
    make_audit(
        north,
        "Aug26",
        date(2026, 8, 30),
        admin_user,
        lines=[
            ("A1", 10, 15, 10),  # excess 50
            ("A2", 10, 9, 50),  # within tolerance: shortage 50 still counts
            ("A3", 10, 0, 3),  # shortage 30
            ("A4", 10, None, 99),  # not counted: left out
        ],
    )
    make_audit(
        north,
        "Frozen",
        date(2026, 8, 31),
        admin_user,
        stage=Audit.Stage.ERP_FROZEN,
        lines=[("A1", 10, 100, 10)],
    )
    rows = dashboards.variance_rows(filters_for(branches))
    assert len(rows) == 1
    row = rows[0]
    assert (row["excess"], row["shortage"], row["net"]) == (
        Decimal("50.00"),
        Decimal("80.00"),
        Decimal("-30.00"),
    )
    assert row["month"] == date(2026, 8, 1)


def test_variance_by_branch_and_month(admin_user, branches, north, south):
    make_audit(north, "N-Jul", date(2026, 7, 2), admin_user, lines=[("A1", 10, 20, 1)])
    make_audit(south, "S-Jul", date(2026, 7, 9), admin_user, lines=[("A1", 10, 0, 1)])
    make_audit(north, "N-Aug", date(2026, 8, 2), admin_user, lines=[("A1", 10, 13, 1)])
    rows = dashboards.variance_rows(filters_for(branches))
    assert [(r["month"].month, r["branch"], r["net"]) for r in rows] == [
        (7, "N1", Decimal(10)),
        (7, "S1", Decimal(-10)),
        (8, "N1", Decimal(3)),
    ]
    totals = dashboards.variance_by_month(rows)
    assert [(t["excess"], t["shortage"], t["net"]) for t in totals] == [
        (Decimal(10), Decimal(10), Decimal(0)),
        (Decimal(3), Decimal(0), Decimal(3)),
    ]


# ---------- repeat items ----------


def test_repeat_items_streaks(admin_user, branches, north, south):
    # North: A1 short in 3 audits in a row; B1 short, OK, short (gap breaks it).
    make_audit(
        north, "N1", date(2026, 5, 1), admin_user, lines=[("A1", 10, 0, 1), ("B1", 10, 0, 1)]
    )
    make_audit(
        north, "N2", date(2026, 6, 1), admin_user, lines=[("A1", 10, 0, 1), ("B1", 10, 10, 1)]
    )
    make_audit(
        north, "N3", date(2026, 7, 1), admin_user, lines=[("A1", 10, 5, 1), ("B1", 10, 0, 1)]
    )
    # South: A1 flagged once only; North's A1 must not join with it.
    make_audit(south, "S1", date(2026, 8, 1), admin_user, lines=[("A1", 10, 0, 1)])
    rows = dashboards.repeat_items(filters_for(branches))
    assert [(r["branch_code"], r["item_code"], r["streak"]) for r in rows] == [("N1", "A1", 3)]
    assert rows[0]["total_value"] == Decimal(-25)
    last_query = Query.objects.get(audit__title="N3", line__item_code="A1")
    assert rows[0]["query_id"] == last_query.pk


def test_repeat_tab_page(admin_client, admin_user, north):
    today = timezone.localdate()
    make_audit(north, "N1", today - timedelta(days=40), admin_user, lines=[("A1", 10, 0, 1)])
    make_audit(north, "N2", today, admin_user, lines=[("A1", 10, 0, 1)])
    body = dash(admin_client, "repeat").content.decode()
    assert "A1" in body


# ---------- trend ----------


def test_trend_for_branch_and_item(admin_user, branches, north, south, company_a):
    make_audit(
        north, "N-May", date(2026, 5, 1), admin_user, lines=[("A1", 10, 20, 1), ("B1", 10, 0, 1)]
    )
    make_audit(north, "N-Jun", date(2026, 6, 1), admin_user, lines=[("B1", 10, 10, 1)])
    make_audit(south, "S-Jun", date(2026, 6, 2), admin_user, lines=[("A1", 10, 7, 1)])
    filters = filters_for(branches)
    branch_rows = dashboards.trend_rows(filters, branch=north)
    assert [(r["label"], r["net"], r["queries"]) for r in branch_rows] == [
        ("N1 N-May", Decimal(0), 2),
        ("N1 N-Jun", Decimal(0), 0),
    ]
    item = dashboards.find_item(company_a, "a1")
    item_rows = dashboards.trend_rows(filters, item=item)
    assert [(r["label"], r["net"]) for r in item_rows] == [
        ("N1 N-May", Decimal(10)),
        ("S1 S-Jun", Decimal(-3)),
    ]


def test_trend_tab_unknown_item(admin_client, admin_user, north):
    make_audit(north, "N-May", timezone.localdate(), admin_user, lines=[("A1", 10, 20, 1)])
    body = dash(admin_client, "trend", item="ZZZ").content.decode()
    assert "No item “ZZZ”" in body
    csv_text = b"".join(
        dash(admin_client, "trend", item="A1", export="csv").streaming_content
    ).decode("utf-8-sig")
    assert "N1 N-May" in csv_text


def test_variance_tab_csv(admin_client, admin_user, north):
    make_audit(north, "N-May", timezone.localdate(), admin_user, lines=[("A1", 10, 20, 1)])
    csv_text = b"".join(dash(admin_client, "variance", export="csv").streaming_content).decode(
        "utf-8-sig"
    )
    assert csv_text.splitlines()[0] == "Month,Branch,Excess value,Shortage value,Net variance"
    assert ",N1,10.00,0.00,10.00" in csv_text


# ---------- backup ----------


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    (root / "attachments" / "2026" / "11").mkdir(parents=True)
    (root / "attachments" / "2026" / "11" / "photo.jpg").write_bytes(b"jpeg-bytes")
    (root / "logos").mkdir()
    (root / "logos" / "logo.png").write_bytes(b"png-bytes")
    with override_settings(MEDIA_ROOT=root):
        yield root


@pytest.fixture
def no_log(monkeypatch):
    """Transactional tests (real SQLite backup needs no open transaction) can't clean up the
    activity log afterwards, because the log refuses deletes. So skip writing it there."""
    monkeypatch.setattr("core.backup_views.log_activity", lambda *args, **kwargs: None)


def download(client, kind):
    response = client.post(reverse("core:backup_download"), {"kind": kind})
    assert response.status_code == 200, response.content[:300]
    return zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))


def test_backup_admin_only(branches, company_a, viewer):
    client = client_for(viewer)
    assert client.get(reverse("core:backup")).status_code == 403
    assert client.post(reverse("core:backup_download"), {"kind": "database"}).status_code == 403
    assert not BackupLog.objects.exists()


def test_backup_get_not_allowed(admin_client):
    assert admin_client.get(reverse("core:backup_download")).status_code == 405


@pytest.mark.django_db(transaction=True)
def test_backup_database_only(no_log, admin_client, admin_user, media_root):
    Company.objects.create(name="Zeta Co")
    archive = download(admin_client, "database")
    names = set(archive.namelist())
    assert names == {backup.INFO_NAME, backup.DB_NAME}
    info = archive.read(backup.INFO_NAME).decode()
    assert "environment: test" in info and "kind: database" in info
    # The copied database is a real, readable database with our data.
    db_path = Path(archive.extract(backup.DB_NAME, media_root.parent / "check"))
    backup.check_database(db_path)
    import sqlite3

    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT name FROM core_company").fetchall() == [("Zeta Co",)]
    record = BackupLog.objects.get()
    assert (record.kind, record.created_by) == ("database", admin_user)


def test_backup_download_is_logged(admin_client, monkeypatch, media_root):
    # Inside a test transaction the real SQLite copy can't run, so a stand-in copy is used.
    import sqlite3

    def fake_copy(target):
        copy = sqlite3.connect(target)
        copy.execute("CREATE TABLE django_migrations (id integer)")
        copy.close()

    monkeypatch.setattr(backup, "copy_database", fake_copy)
    download(admin_client, "database")
    entry = ActivityLog.objects.get(action="backup downloaded")
    assert entry.changes["kind"] == "Database only"


@pytest.mark.django_db(transaction=True)
def test_backup_with_media(no_log, admin_client, media_root):
    archive = download(admin_client, "full")
    assert {"media/attachments/2026/11/photo.jpg", "media/logos/logo.png"} <= set(
        archive.namelist()
    )
    assert archive.read("media/logos/logo.png") == b"png-bytes"


@pytest.mark.django_db(transaction=True)
def test_backup_temp_files_removed(no_log, admin_client, media_root, monkeypatch, tmp_path):
    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(work))
    download(admin_client, "full")
    assert list(work.iterdir()) == []


def test_backup_bad_kind(admin_client):
    response = admin_client.post(reverse("core:backup_download"), {"kind": "everything"})
    assert response.status_code == 302
    assert not BackupLog.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_backup_page_shows_last(no_log, admin_client, admin_user, media_root):
    assert (
        "No backup has been downloaded yet"
        in admin_client.get(reverse("core:backup")).content.decode()
    )
    download(admin_client, "database")
    body = admin_client.get(reverse("core:backup")).content.decode()
    assert "Database only" in body and "No backup has been downloaded yet" not in body


# ---------- reminder banner ----------


def test_banner_never_backed_up(admin_client):
    assert 'id="backup-reminder"' in admin_client.get(reverse("core:home")).content.decode()


def test_banner_after_seven_days_and_hidden_after_backup(admin_client, admin_user):
    record = BackupLog.objects.create(created_by=admin_user, kind="database", size=1, file_name="x")
    BackupLog.objects.filter(pk=record.pk).update(created_at=timezone.now() - timedelta(days=9))
    body = admin_client.get(reverse("core:home")).content.decode()
    assert "No backup for 9 days" in body
    BackupLog.objects.create(created_by=admin_user, kind="database", size=1, file_name="y")
    assert 'id="backup-reminder"' not in admin_client.get(reverse("core:home")).content.decode()


def test_banner_six_days_not_shown(admin_user):
    assert backup.reminder() == {"days": None}  # never backed up
    BackupLog.objects.create(created_by=admin_user, kind="full", size=1, file_name="x")
    assert backup.reminder(now=timezone.now() + timedelta(days=6, hours=23)) is None
    assert backup.reminder(now=timezone.now() + timedelta(days=7, hours=1)) == {"days": 7}


def test_banner_never_for_non_admin(viewer):
    assert (
        'id="backup-reminder"' not in client_for(viewer).get(reverse("core:home")).content.decode()
    )


# ---------- restore ----------


def make_zip(tmp_path, admin_client):
    archive_bytes = b"".join(
        admin_client.post(reverse("core:backup_download"), {"kind": "full"}).streaming_content
    )
    path = tmp_path / "backup.zip"
    path.write_bytes(archive_bytes)
    return path


@pytest.mark.django_db(transaction=True)
def test_restore_round_trip(no_log, tmp_path, media_root):
    admin = make_user("boss", is_admin=True)
    Company.objects.create(name="Kept Co")
    zip_path = make_zip(tmp_path, client_for(admin))
    # Changes after the backup: these must disappear.
    Company.objects.create(name="Added Later")
    Company.objects.filter(name="Kept Co").update(name="Renamed")
    (media_root / "logos" / "logo.png").write_bytes(b"changed")

    out = io.StringIO()
    call_command(
        "restore_backup", str(zip_path), no_input=True, old_copy_dir=tmp_path / "old", stdout=out
    )

    assert sorted(Company.objects.values_list("name", flat=True)) == ["Kept Co"]
    assert (media_root / "logos" / "logo.png").read_bytes() == b"png-bytes"
    assert (media_root / "attachments" / "2026" / "11" / "photo.jpg").exists()
    assert list((tmp_path / "old").glob("test.before-restore-*.db"))
    assert list(media_root.parent.glob("media.before-restore-*"))
    assert "Restore finished" in out.getvalue()


def test_restore_refused_on_production(tmp_path):
    with (
        override_settings(DJANGO_ENV="production"),
        pytest.raises(CommandError, match="production"),
    ):
        call_command("restore_backup", str(tmp_path / "any.zip"), no_input=True)


def test_restore_bad_zip(tmp_path, db):
    not_zip = tmp_path / "notes.zip"
    not_zip.write_text("hello")
    with pytest.raises(CommandError, match="not a zip"):
        call_command("restore_backup", str(not_zip), no_input=True)
    other_zip = tmp_path / "other.zip"
    with zipfile.ZipFile(other_zip, "w") as archive:
        archive.writestr("readme.txt", "hi")
    with pytest.raises(CommandError, match="not a Stock Audit backup"):
        call_command("restore_backup", str(other_zip), no_input=True)


def test_restore_refuses_unsafe_names(tmp_path, db):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr(backup.INFO_NAME, "x")
        archive.writestr(backup.DB_NAME, "x")
        archive.writestr("media/../../outside.txt", "x")
    with pytest.raises(CommandError, match="Unexpected file"):
        call_command("restore_backup", str(evil), no_input=True)


@pytest.mark.parametrize(
    "name", ["media/C:/outside.txt", "media/C:outside.txt", r"media/..\outside.txt", "/etc/x"]
)
def test_unsafe_media_names_refused(name):
    assert not backup.is_safe_media_name(name)


def test_restore_refuses_drive_letter(tmp_path, db):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr(backup.INFO_NAME, "x")
        archive.writestr(backup.DB_NAME, "x")
        archive.writestr("media/C:/outside.txt", "x")
    with pytest.raises(CommandError, match="Unexpected file"):
        call_command("restore_backup", str(evil), no_input=True)


def test_media_target_stays_inside(tmp_path):
    root = tmp_path / "media"
    assert backup.media_target(root, "media/a/b.jpg") == (root / "a" / "b.jpg").resolve()
    with pytest.raises(backup.BackupError):
        backup.media_target(root, "media/../x.txt")


def test_backup_not_logged_until_whole_file_sent(admin_client, monkeypatch, media_root, tmp_path):
    import sqlite3

    def fake_copy(target):
        sqlite3.connect(target).close()

    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(work))
    monkeypatch.setattr(backup, "copy_database", fake_copy)
    response = admin_client.post(reverse("core:backup_download"), {"kind": "database"})
    next(iter(response.streaming_content))  # download starts, then the user cancels
    response.close()
    assert not BackupLog.objects.exists()  # the reminder stays on
    assert list(work.iterdir()) == []  # and no database copy is left in temp


def test_backup_temp_removed_when_logging_setup_fails(admin_client, monkeypatch, tmp_path):
    import sqlite3

    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(work))
    monkeypatch.setattr(backup, "copy_database", lambda target: sqlite3.connect(target).close())

    def broken(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr("core.backup_views.StreamingHttpResponse", broken)
    admin_client.raise_request_exception = False
    assert (
        admin_client.post(reverse("core:backup_download"), {"kind": "database"}).status_code == 500
    )
    assert list(work.iterdir()) == []


def test_restore_damaged_database(tmp_path, db):
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr(backup.INFO_NAME, "x")
        archive.writestr(backup.DB_NAME, "not a database at all" * 100)
    with pytest.raises(CommandError, match="database in the backup"):
        call_command("restore_backup", str(broken), no_input=True)
    assert settings.DJANGO_ENV == "test"
