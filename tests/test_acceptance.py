"""Version 1 acceptance criteria (docs/project.md, section 6), one test each.

Every test walks the step through the screens with the Django test client, the way a pilot
user would. Only the starting team (company, Branches, users with rights) is set up directly.

The story: Alpha Traders has Branches North and South. In North, Priya uploads ERP stock,
Mohan (Manager) freezes it, Anil counts, Rita replies to queries, Vikram adds actions and
closes queries, Deepa owns actions, and Lata locks the audit.
"""

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from accounts.models import Membership, User
from accounts.rights import Role
from audits.models import Action, Audit, AuditLine, Item, Query
from core.models import ActivityLog, Branch, Company

from .conftest import PASSWORD, client_for, make_user
from .test_audits import csv_file, grant, xlsx_file

D = Decimal
S = AuditLine.Status
QS = Query.Status

# Aug26 ERP stock for North. Tolerance will be 2 qty and 5 %.
AUG_ROWS = [
    ["A100", "Steel bolt 10mm", "PCS", 100, 2.5],  # counted 90 in app   -> Shortage
    ["A200", "Paint 1L", "LTR", 5, 120],  # counted 5 on sheet  -> OK
    ["A300", "Hex nut M8", "PCS", 50, 10],  # counted 60 on sheet -> Excess
    ["A400", "Washer 8mm", "PCS", 20, 1],  # never counted       -> Not counted
    ["A500", "Drill bit", "PCS", 10, 30],  # counted 10 in app   -> OK
]


@pytest.fixture(autouse=True)
def media_in_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


# ---------- team (setup only) ----------


@pytest.fixture
def north(branches):
    return branches[0]


@pytest.fixture
def team(north, company_a):
    """One user per job in North, each with only the rights that job needs."""

    def person(name, role, **rights):
        user = make_user(name)
        grant(user, north, company_a, role=role, **rights)
        return client_for(user)

    return {
        "priya": person("priya", Role.USER, upload_erp=True),
        "mohan": person("mohan", Role.MANAGER, set_tolerance_freeze=True),
        "anil": person("anil", Role.AUDITOR, count_and_remarks=True),
        "rita": person("rita", Role.USER, reply_queries=True),
        "vikram": person("vikram", Role.MANAGER, manage_actions=True, verify_close_queries=True),
        "deepa": person("deepa", Role.VIEWER, view_reports=True),
        "lata": person("lata", Role.MANAGER, lock_audit=True, view_reports=True),
    }


# ---------- steps through the screens ----------


def url(name, *args):
    return reverse(f"audits:{name}", args=args)


def client_for_login(username, password):
    client = Client()
    response = client.post(reverse("accounts:login"), {"username": username, "password": password})
    assert response.status_code == 302, response.content[:300]
    return client


def create_audit(client, branch, title, audit_date, stock_as_on):
    response = client.post(
        url("audit_create"),
        {
            "branch": branch.pk,
            "title": title,
            "audit_date": audit_date,
            "audit_type": "scheduled",
            "stock_as_on": stock_as_on,
        },
    )
    audit = Audit.objects.get(branch=branch, title=title)
    assert response.url == url("audit_detail", audit.pk)
    return audit


def upload_erp(client, audit, file):
    return client.post(url("erp_upload", audit.pk), {"file": file})


def confirm_pending_upload(client, audit):
    pending = audit.uploads.get(status="pending")
    return client.post(url("upload_preview", pending.pk))


def freeze(client, audit, qty="2", pct="5"):
    client.post(
        url("audit_freeze", audit.pk), {"qty_tolerance": qty, "pct_tolerance": pct, "confirm": "on"}
    )
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.ERP_FROZEN


def count(client, audit, code, value):
    line = audit.lines.get(item_code=code)
    return client.post(url("line_count", line.pk), {"physical_qty": value})


def count_sheet_file(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Item No", "Description", "UoM", "Physical Qty", "Remark"])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile("count.xlsx", buffer.getvalue())


def checking_done(client, audit):
    client.post(url("checking_done", audit.pk), {"confirm": "on"})
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.CHECKING_DONE


def lock(client, audit):
    client.post(url("audit_lock", audit.pk), {"confirm": "on"})
    audit.refresh_from_db()


def line_snapshot(audit):
    return list(audit.lines.order_by("item_code").values_list("item_code", "erp_qty", "rate"))


def counts_snapshot(audit):
    return list(audit.lines.order_by("item_code").values_list("item_code", "physical_qty"))


# Each stage builds on the one before, so later criteria reuse the earlier walk-throughs.


def aug26_frozen(team, north):
    audit = create_audit(team["priya"], north, "Aug26", "2026-08-30", "2026-08-29")
    upload_erp(team["priya"], audit, xlsx_file(AUG_ROWS))
    confirm_pending_upload(team["priya"], audit)
    freeze(team["mohan"], audit)
    return audit


def aug26_counted(team, north):
    audit = aug26_frozen(team, north)
    anil = team["anil"]
    count(anil, audit, "A100", "90")
    count(anil, audit, "A500", "10")
    anil.post(
        url("count_sheet_upload", audit.pk),
        {"file": count_sheet_file([["A200", "", "", 5, ""], ["A300", "", "", 60, "Extra box"]])},
    )
    pending = audit.count_uploads.get(status="pending")
    anil.post(url("count_sheet_preview", pending.pk), {"expected_changes": 0})
    return audit


def aug26_checked(team, north):
    audit = aug26_counted(team, north)
    checking_done(team["anil"], audit)
    return audit


# ---------- 1 ----------


def test_criterion_01_admin_sets_different_rights_in_two_branches(
    admin_client, company_a, branches
):
    """Admin creates users and sets different rights for the same user in two Branches."""
    north, south = branches
    admin_client.post(
        reverse("accounts:user_create"),
        {
            "username": "kiran",
            "full_name": "Kiran Rao",
            "email": "kiran@example.com",
            "temporary_password": "Temp-pass-1",
        },
    )
    kiran = User.objects.get(username="kiran")
    admin_client.post(
        reverse("accounts:membership_add", args=[kiran.pk]),
        {"company": company_a.pk, "role": Role.USER},
    )
    membership = Membership.objects.get(user=kiran, company=company_a)
    admin_client.post(
        reverse("accounts:rights_grid", args=[membership.pk]),
        {
            f"r_{north.pk}_upload_erp": "on",
            f"r_{north.pk}_view_reports": "on",
            f"r_{south.pk}_view_reports": "on",
        },
    )

    # Kiran logs in with the temporary password and must change it first.
    kiran_client = client_for_login("kiran", "Temp-pass-1")
    kiran_client.post(
        reverse("accounts:password_change"),
        {"old_password": "Temp-pass-1", "new_password1": PASSWORD, "new_password2": PASSWORD},
    )

    # North: may create audits. South: may only look.
    form = kiran_client.get(url("audit_create")).context["form"]
    assert list(form.fields["branch"].queryset) == [north]
    create_audit(kiran_client, north, "Aug26", "2026-08-30", "2026-08-29")
    response = kiran_client.post(
        url("audit_create"),
        {
            "branch": south.pk,
            "title": "Aug26",
            "audit_date": "2026-08-30",
            "audit_type": "scheduled",
            "stock_as_on": "2026-08-29",
        },
    )
    assert response.status_code == 200 and not Audit.objects.filter(branch=south).exists()

    south_audit = create_audit(admin_client, south, "Sep26", "2026-09-30", "2026-09-29")
    assert kiran_client.get(url("audit_detail", south_audit.pk)).status_code == 200
    assert upload_erp(kiran_client, south_audit, xlsx_file(AUG_ROWS)).status_code == 403
    assert ActivityLog.objects.filter(action="rights").count() == 2


# ---------- 2 ----------


def test_criterion_02_upload_with_errors_correct_replace_confirm(team, north):
    """A user creates an audit, uploads an ERP file with errors, corrects it, replaces the
    draft, and confirms."""
    priya = team["priya"]
    audit = create_audit(priya, north, "Aug26", "2026-08-30", "2026-08-29")

    bad_rows = [["A100", "Steel bolt", "PCS", "abc", 2.5], ["A100", "Steel bolt", "PCS", 5, 2.5]]
    page = upload_erp(priya, audit, xlsx_file(bad_rows)).content.decode()
    assert "is not a number" in page and "is repeated" in page and "Nothing was saved" in page
    assert not audit.lines.exists() and not audit.uploads.exists()

    # Corrected file: preview first, nothing saved until confirmed.
    upload_erp(priya, audit, xlsx_file(AUG_ROWS[:2]))
    assert not audit.lines.exists()
    confirm_pending_upload(priya, audit)
    assert sorted(audit.lines.values_list("item_code", flat=True)) == ["A100", "A200"]

    # A fuller file (CSV this time) replaces the draft.
    text = "Item No,Description,UoM,ERP Qty,Rate p.u.\n" + "".join(
        f"{code},{desc},{uom},{qty},{rate}\n" for code, desc, uom, qty, rate in AUG_ROWS
    )
    upload_erp(priya, audit, csv_file(text))
    confirm_pending_upload(priya, audit)

    assert [code for code, _qty, _rate in line_snapshot(audit)] == [r[0] for r in AUG_ROWS]
    assert sorted(audit.uploads.values_list("status", flat=True)) == ["replaced", "used"]
    page = priya.get(url("audit_detail", audit.pk)).content.decode()
    assert "A500" in page and "Drill bit" in page
    assert ActivityLog.objects.filter(action="replace ERP file").exists()


# ---------- 3 ----------


def test_criterion_03_freeze_with_tolerance_locks_erp_lines(team, north):
    """A Manager sets tolerance (quantity and percentage) and freezes ERP stock; the ERP
    lines can no longer be changed."""
    audit = aug26_frozen(team, north)
    assert (audit.qty_tolerance, audit.pct_tolerance) == (D(2), D(5))
    before = line_snapshot(audit)

    priya = team["priya"]
    new_rows = [["Z1", "Other", "PCS", 1, 1]]
    assert upload_erp(priya, audit, xlsx_file(new_rows)).status_code == 403
    assert priya.get(url("audit_edit", audit.pk)).status_code == 403
    assert team["mohan"].get(url("audit_freeze", audit.pk)).status_code == 403  # no 2nd freeze
    assert line_snapshot(audit) == before
    assert not audit.uploads.filter(status="pending").exists()
    assert ActivityLog.objects.filter(action="freeze ERP stock").exists()


# ---------- 4 ----------


def test_criterion_04_count_in_app_and_sheet_some_not_counted(team, north):
    """The auditor counts in the app and through the offline Excel count sheet, and some
    lines are left Not counted."""
    audit = aug26_frozen(team, north)
    anil = team["anil"]

    # Offline sheet: download is blind (no ERP qty), fill some lines, upload, confirm.
    sheet = load_workbook(io.BytesIO(anil.get(url("count_sheet", audit.pk)).content)).active
    assert [row[0] for row in sheet.iter_rows(min_row=2, values_only=True)] == [
        r[0] for r in AUG_ROWS
    ]
    assert "ERP Qty" not in str(list(sheet.values))

    count(anil, audit, "A100", "90")
    count(anil, audit, "A500", "10")
    anil.post(
        url("count_sheet_upload", audit.pk),
        {"file": count_sheet_file([["A200", "", "", 5, ""], ["A300", "", "", 60, "Extra box"]])},
    )
    pending = audit.count_uploads.get(status="pending")
    assert (
        anil.post(url("count_sheet_preview", pending.pk), {"expected_changes": 0}).status_code
        == 302
    )

    statuses = dict(audit.lines.values_list("item_code", "count_status"))
    assert statuses == {
        "A100": S.SHORTAGE,
        "A200": S.OK,
        "A300": S.EXCESS,
        "A400": S.NOT_COUNTED,
        "A500": S.OK,
    }
    stats = anil.get(url("count_stats", audit.pk)).context["summary"]
    assert (stats["counted"], stats["not_counted"]) == (4, 1)
    listing = anil.get(url("audit_detail", audit.pk), {"count_status": "not_counted"})
    assert [row.item_code for row in listing.context["table"].page] == ["A400"]
    assert ActivityLog.objects.filter(action="count").count() == 2
    assert ActivityLog.objects.filter(action="count (sheet)").count() == 2


# ---------- 5 ----------


def test_criterion_05_checking_done_raises_queries_and_fixes_counts(team, north):
    """At Checking Done, queries are created only for Excess and Shortage lines, and counts
    can no longer be edited or rolled back."""
    audit = aug26_counted(team, north)
    checking_done(team["anil"], audit)

    queries = {q.line.item_code: q for q in Query.objects.filter(audit=audit)}
    assert set(queries) == {"A100", "A300"}  # Shortage and Excess only
    assert all(q.status == QS.OPEN for q in queries.values())
    page = team["anil"].get(url("audit_detail", audit.pk)).content.decode()
    assert "A100" in page and "A300" in page

    before = counts_snapshot(audit)
    anil = team["anil"]
    assert count(anil, audit, "A200", "1").status_code == 403  # edit
    assert count(anil, audit, "A100", "").status_code == 403  # clear back to Not counted
    sheet = count_sheet_file([["A400", "", "", 20, ""]])
    assert anil.post(url("count_sheet_upload", audit.pk), {"file": sheet}).status_code == 403
    assert anil.post(url("checking_done", audit.pk), {"confirm": "on"}).status_code == 403
    assert counts_snapshot(audit) == before
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.CHECKING_DONE
    assert Query.objects.count() == 2


# ---------- 6 ----------


def test_criterion_06_query_open_to_closed_with_owner_and_verifier(team, north):
    """A query goes through Open, Reply Submitted, Action Pending, and Closed, with an action
    owner and verifier."""
    audit = aug26_checked(team, north)
    query = Query.objects.get(audit=audit, line__item_code="A100")
    deepa = User.objects.get(username="deepa")
    seen = [query.status]

    def status_now():
        query.refresh_from_db()
        return query.status

    team["rita"].post(url("query_reply", query.pk), {"text": "Ten pcs sent to South."})
    seen.append(status_now())
    team["vikram"].post(
        url("action_add", query.pk),
        {"description": "Raise transfer slip", "owner": deepa.pk, "due_date": ""},
    )
    seen.append(status_now())
    action = Action.objects.get(query=query)

    team["vikram"].post(url("query_close", query.pk), {"note": ""})  # blocked: not verified
    assert status_now() == QS.ACTION_PENDING
    team["deepa"].post(url("action_step", action.pk, "done"))
    team["vikram"].post(url("action_step", action.pk, "verify"))
    team["vikram"].post(url("query_close", query.pk), {"note": ""})
    seen.append(status_now())

    assert seen == [QS.OPEN, QS.REPLY_SUBMITTED, QS.ACTION_PENDING, QS.CLOSED]
    action.refresh_from_db()
    assert action.owner == deepa and action.done_by == deepa
    assert action.status == Action.Status.VERIFIED and action.verified_by.username == "vikram"
    assert query.closed_by.username == "vikram"
    page = team["deepa"].get(url("query_detail", query.pk)).content.decode()
    assert "Closed" in page and "Raise transfer slip" in page


# ---------- 7 ----------


def test_criterion_07_open_query_carries_forward_with_item_history(team, north):
    """An open query carries into the next audit and shows the item history table."""
    aug = aug26_checked(team, north)
    open_query = Query.objects.get(audit=aug, line__item_code="A100")

    sep = create_audit(team["priya"], north, "Sep26", "2026-09-30", "2026-09-29")
    upload_erp(team["priya"], sep, xlsx_file(AUG_ROWS[:2]))
    confirm_pending_upload(team["priya"], sep)
    freeze(team["mohan"], sep)
    count(team["anil"], sep, "A100", "95")

    detail = team["vikram"].get(url("audit_detail", sep.pk))
    assert open_query in detail.context["audit_queries"]
    page = detail.content.decode()
    assert "Carried" in page and "Aug26" in page
    assert Query.objects.filter(line__item_code="A100").count() == 1  # carried, not copied

    response = team["vikram"].get(url("query_detail", open_query.pk), {"tab": "history"})
    history = response.context["history"]
    assert [row.audit.title for row in history] == ["Aug26", "Sep26"]
    assert [row.physical_qty for row in history] == [D(90), D(95)]
    page = response.content.decode()
    assert "Item history" in page and "Sep26" in page


# ---------- 8 ----------


def test_criterion_08_lock_reopen_with_reason_all_logged(team, north, admin_client):
    """The audit is locked; Admin reopens it with a reason; every step appears in the
    activity log."""
    audit = aug26_checked(team, north)
    lock(team["lata"], audit)
    assert audit.stage == Audit.Stage.LOCKED

    reopen = url("audit_reopen", audit.pk)
    assert team["lata"].post(reopen, {"reason": "x"}).status_code == 403  # Admin only
    assert "A reason is required" in admin_client.post(reopen, {"reason": ""}).content.decode()
    admin_client.post(reopen, {"reason": "Count sheet was wrong for A300"})
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.CHECKING_DONE

    audit_steps = set(
        ActivityLog.objects.filter(record_type="Audit", record_id=str(audit.pk)).values_list(
            "action", flat=True
        )
    )
    assert {
        "create",
        "upload ERP file",
        "freeze ERP stock",
        "checking done",
        "lock",
        "reopen",
    } <= audit_steps
    for step in ("count", "count (sheet)"):
        assert ActivityLog.objects.filter(action=step).exists(), step
    assert ActivityLog.objects.get(action="reopen").changes["reason"][1] == (
        "Count sheet was wrong for A300"
    )

    log_page = admin_client.get(reverse("core:activity_log"), {"q": "Count sheet was wrong"})
    text = log_page.content.decode()
    assert "reopen" in text and "Count sheet was wrong for A300" in text
    full_log = admin_client.get(reverse("core:activity_log")).content.decode()
    assert ">lock<" in full_log and ">checking done<" in full_log


# ---------- 9 ----------


def test_criterion_09_pdf_and_excel_match_screen(team, north):
    """The PDF and Excel exports match the data on screen."""
    audit = aug26_checked(team, north)
    lata = team["lata"]
    lock(lata, audit)
    screen = lata.get(url("audit_detail", audit.pk)).context["summary"]

    book = load_workbook(io.BytesIO(lata.get(url("audit_excel", audit.pk)).content))
    rows = list(book["Lines"].values)[1:]
    assert len(rows) == audit.lines.count() == screen["total"] == len(AUG_ROWS)
    db_value = sum(line.erp_value for line in audit.lines.all())
    assert sum(D(str(row[5])) for row in rows) == db_value == D("1670.00")
    excel_status = [row[9] for row in rows]
    assert excel_status.count("Not counted") == screen["not_counted"] == 1
    assert excel_status.count("Excess") == screen["excess"]
    assert excel_status.count("Shortage") == screen["shortage"]
    variance_total = sum(D(str(row[8])) for row in rows if row[8] is not None)
    assert variance_total == screen["excess_value"] + screen["shortage_value"]
    a100 = next(row for row in rows if row[0] == "A100")
    assert (a100[3], a100[6], a100[7], a100[9]) == (100, 90, -10, "Shortage")
    query_rows = list(book["Queries"].values)[1:]
    assert sorted(row[0] for row in query_rows) == ["A100", "A300"]

    response = lata.get(url("audit_pdf", audit.pk))
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    text = response.content.decode("latin-1")
    assert "DRAFT" not in text  # locked
    assert "Not counted \\(1 lines\\)" in text and "A400" in text
    assert "Variances \\(2 lines\\)" in text
    assert "1,670.00" in text


# ---------- 10 ----------


@pytest.fixture
def no_log(monkeypatch):
    """Transactional test (the real SQLite copy needs no open transaction): the activity log
    refuses deletes, so it couldn't be cleaned up afterwards. Skip writing it here."""
    monkeypatch.setattr("core.backup_views.log_activity", lambda *args, **kwargs: None)


@pytest.mark.django_db(transaction=True)
def test_criterion_10_backup_zip_restores(no_log, settings, tmp_path):
    """Admin downloads a backup zip and it restores correctly on a test server."""
    media_root = tmp_path / "media"
    (media_root / "logos").mkdir(parents=True)
    (media_root / "logos" / "logo.png").write_bytes(b"png-bytes")
    settings.MEDIA_ROOT = media_root

    # Setup without the activity log (see no_log): a company, a Branch and an audit line.
    admin = make_user("boss", is_admin=True)
    company = Company.objects.create(name="Alpha Traders")
    north = Branch.objects.create(company=company, name="North", code="N1")
    audit = Audit.objects.create(
        branch=north,
        title="Aug26",
        audit_date=date(2026, 8, 30),
        stock_as_on=date(2026, 8, 29),
        created_by=admin,
    )
    item = Item.objects.create(company=company, code="A100", description="Bolt", uom="PCS")
    AuditLine.objects.create(
        audit=audit,
        item=item,
        item_code="A100",
        description="Bolt",
        uom="PCS",
        erp_qty=D(100),
        rate=D("2.50"),
        erp_value=D("250.00"),
    )

    response = client_for(admin).post(reverse("core:backup_download"), {"kind": "full"})
    assert response.status_code == 200
    zip_bytes = b"".join(response.streaming_content)
    archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    assert "media/logos/logo.png" in archive.namelist()
    zip_path = tmp_path / "backup.zip"
    zip_path.write_bytes(zip_bytes)

    # Things change after the backup; the restore must undo them.
    Company.objects.create(name="Added Later")
    AuditLine.objects.filter(audit=audit).update(erp_qty=D(1))
    (media_root / "logos" / "logo.png").write_bytes(b"changed")

    out = io.StringIO()
    call_command(
        "restore_backup", str(zip_path), no_input=True, old_copy_dir=tmp_path / "old", stdout=out
    )
    assert "Restore finished" in out.getvalue()

    assert list(Company.objects.values_list("name", flat=True)) == ["Alpha Traders"]
    assert (media_root / "logos" / "logo.png").read_bytes() == b"png-bytes"
    restored = Audit.objects.get(title="Aug26")
    detail = client_for(User.objects.get(username="boss")).get(url("audit_detail", restored.pk))
    assert detail.status_code == 200
    assert [line.erp_qty for line in detail.context["table"].page] == [D(100)]
