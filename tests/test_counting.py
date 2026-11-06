import io
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from accounts.rights import Role
from audits.counting import work_out
from audits.models import Audit, AuditLine, CountUpload, Item, Query, Remark
from core.models import ActivityLog

from .conftest import client_for, make_user
from .test_audits import audit, grant, manager, priya  # noqa: F401

D = Decimal
S = AuditLine.Status


# ---------- fixtures ----------


@pytest.fixture
def anil(branches, company_a):
    """Auditor with the Count right in North."""
    user = make_user("anil")
    grant(user, branches[0], company_a, role=Role.AUDITOR, count_and_remarks=True)
    return user


@pytest.fixture
def sita(branches, company_a):
    """Second auditor in North."""
    user = make_user("sita")
    grant(user, branches[0], company_a, role=Role.AUDITOR, count_and_remarks=True)
    return user


@pytest.fixture
def frozen(audit, manager, company_a):  # noqa: F811
    """Audit Aug26, frozen with qty tolerance 2, three lines."""
    for code, qty, rate in (("A100", 100, "2.50"), ("A200", 5, "120"), ("A300", 0, "10")):
        item = Item.objects.create(
            company=company_a, code=code, description=f"Item {code}", uom="PCS"
        )
        AuditLine.objects.create(
            audit=audit,
            item=item,
            item_code=code,
            description=item.description,
            uom="PCS",
            erp_qty=D(qty),
            rate=D(rate),
            erp_value=D(qty) * D(rate),
        )
    audit.stage = Audit.Stage.ERP_FROZEN
    audit.qty_tolerance = D(2)
    audit.frozen_by = manager
    audit.frozen_at = timezone.now()
    audit.save()
    return audit


def line(audit_obj, code):
    return audit_obj.lines.get(item_code=code)


def count(client, audit_obj, code, value, htmx=True):
    extra = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    return client.post(
        reverse("audits:line_count", args=[line(audit_obj, code).pk]),
        {"physical_qty": value},
        **extra,
    )


def sheet(
    rows, headings=("Item No", "Description", "UoM", "Physical Qty", "Remark"), name="count.xlsx"
):
    workbook = Workbook()
    ws = workbook.active
    ws.append(list(headings))
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(name, buffer.getvalue())


def upload_sheet(client, audit_obj, file):
    return client.post(reverse("audits:count_sheet_upload", args=[audit_obj.pk]), {"file": file})


def confirm_sheet(client, audit_obj, accept=False, expected=0):
    pending = CountUpload.objects.get(audit=audit_obj, status=CountUpload.Status.PENDING)
    data = {"expected_changes": expected}
    if accept:
        data["accept_warnings"] = "on"
    return client.post(reverse("audits:count_sheet_preview", args=[pending.pk]), data)


# ---------- status math ----------


@pytest.mark.parametrize(
    "erp, physical, qty_tol, pct_tol, expected",
    [
        ("100", "101", "2", None, S.OK),
        ("100", "102", "2", None, S.OK),
        ("100", "103", "2", None, S.EXCESS),
        ("100", "96", "2", None, S.SHORTAGE),
        ("100", "99", "0", None, S.SHORTAGE),
        ("100", "100", "0", None, S.OK),
        ("100", "101", "5", "0.5", S.EXCESS),  # 1% is over 0.5%
        ("100", "101", "5", "2", S.OK),
        ("0", "5", "10", "5", S.EXCESS),  # ERP 0 + count → over the % test
        ("0", "5", "10", None, S.OK),
        ("0", "0", "0", "5", S.OK),
        ("10", "0", "2", None, S.SHORTAGE),
    ],
)
def test_status_math(erp, physical, qty_tol, pct_tol, expected):
    _var, _value, status = work_out(
        D(erp), D(physical), D("2.5"), D(qty_tol), None if pct_tol is None else D(pct_tol)
    )
    assert status == expected


def test_blank_is_not_counted_and_variance_value():
    assert work_out(D(10), None, D(2), D(0), None) == (None, None, S.NOT_COUNTED)
    assert work_out(D(10), D(7), D("2.5"), D(0), None) == (D(-3), D("-7.50"), S.SHORTAGE)


# ---------- saving a count ----------


def test_save_count_stores_who_when_and_logs(frozen, anil):
    response = count(client_for(anil), frozen, "A100", "96")
    assert "input-success" in response.content.decode()
    assert response["HX-Trigger"] == "counts-changed"
    a100 = line(frozen, "A100")
    assert a100.physical_qty == D(96) and a100.variance_qty == D(-4)
    assert a100.variance_value == D("-10.00") and a100.count_status == S.SHORTAGE
    assert a100.counted_by == anil and a100.counted_at
    log = ActivityLog.objects.get(action="count")
    assert log.changes == {"physical_qty": [None, "96"]}


def test_zero_is_a_count_and_clearing_resets(frozen, anil):
    client = client_for(anil)
    count(client, frozen, "A200", "0")
    assert line(frozen, "A200").count_status == S.SHORTAGE
    count(client, frozen, "A200", "")
    a200 = line(frozen, "A200")
    assert a200.physical_qty is None and a200.count_status == S.NOT_COUNTED
    assert a200.counted_by is None
    assert list(ActivityLog.objects.filter(action="count").values_list("changes", flat=True)) == [
        {"physical_qty": ["0", None]},
        {"physical_qty": [None, "0"]},
    ]


@pytest.mark.parametrize(
    "value, message",
    [("-1", "negative"), ("1.2345", "more than 3 decimals"), ("abc", "is not a number")],
)
def test_bad_counts_are_refused(frozen, anil, value, message):
    response = count(client_for(anil), frozen, "A100", value)
    assert message in response.content.decode()
    assert line(frozen, "A100").physical_qty is None
    assert not ActivityLog.objects.filter(action="count").exists()


def test_same_value_again_is_not_logged_twice(frozen, anil, sita):
    count(client_for(anil), frozen, "A100", "50")
    count(client_for(sita), frozen, "A100", "50.000")
    count(client_for(sita), frozen, "A100", "60")  # last save wins
    a100 = line(frozen, "A100")
    assert a100.physical_qty == D(60) and a100.counted_by == sita
    assert ActivityLog.objects.filter(action="count").count() == 2


def test_count_needs_right_and_branch(frozen, priya, branches, company_a):  # noqa: F811
    assert count(client_for(priya), frozen, "A100", "5", htmx=False).status_code == 403
    south_auditor = make_user("sam")
    grant(south_auditor, branches[1], company_a, role=Role.AUDITOR, count_and_remarks=True)
    assert count(client_for(south_auditor), frozen, "A100", "5").status_code == 404
    assert line(frozen, "A100").physical_qty is None


def test_refused_htmx_count_shows_error_in_row(frozen, anil):
    frozen.stage = Audit.Stage.CHECKING_DONE
    frozen.save()
    response = count(client_for(anil), frozen, "A100", "5")
    assert response.status_code == 200 and "Not saved." in response.content.decode()
    assert line(frozen, "A100").physical_qty is None


def test_count_refused_in_draft_and_after_checking_done(frozen, anil):
    frozen.stage = Audit.Stage.ERP_DRAFT
    frozen.save()
    assert count(client_for(anil), frozen, "A100", "5", htmx=False).status_code == 403
    frozen.stage = Audit.Stage.CHECKING_DONE
    frozen.save()
    assert count(client_for(anil), frozen, "A100", "5", htmx=False).status_code == 403
    assert line(frozen, "A100").physical_qty is None


def test_count_screen_shows_inputs_and_status_filter(frozen, anil):
    client = client_for(anil)
    count(client, frozen, "A100", "90")
    url = reverse("audits:audit_detail", args=[frozen.pk])
    page = client.get(url).content.decode()
    assert 'name="physical_qty"' in page and "Download count sheet" in page
    response = client.get(url, {"count_status": "shortage"})
    assert [row.item_code for row in response.context["table"].page] == ["A100"]


# ---------- count sheet ----------


def test_sheet_download_is_blind(frozen, anil):
    response = client_for(anil).get(reverse("audits:count_sheet", args=[frozen.pk]))
    rows = list(load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))
    assert rows[0] == ("Item No", "Description", "UoM", "Physical Qty", "Remark")
    assert [row[0] for row in rows[1:]] == ["A100", "A200", "A300"]
    assert all(row[3] is None for row in rows[1:])
    assert "ERP Qty" not in str(rows)


@pytest.mark.parametrize(
    "rows, message",
    [
        ([["ZZ-99", "", "", 5, ""]], "Item ZZ-99 is not in this audit"),
        ([["A100", "", "", 5, ""], ["a100", "", "", 6, ""]], "is repeated"),
        ([["A100", "", "", "lots", ""]], "is not a number"),
        ([["A100", "", "", -2, ""]], "negative"),
    ],
)
def test_sheet_errors_save_nothing(frozen, anil, rows, message):
    response = upload_sheet(client_for(anil), frozen, sheet(rows))
    assert message in response.content.decode()
    assert not CountUpload.objects.exists()
    assert not frozen.lines.filter(physical_qty__isnull=False).exists()


def test_sheet_saves_counts_skips_blanks_and_adds_remarks(frozen, anil, sita):
    count(client_for(sita), frozen, "A200", "4")
    Remark.objects.create(audit=frozen, line=line(frozen, "A100"), text="old note", created_by=sita)
    client = client_for(anil)
    upload_sheet(
        client,
        frozen,
        sheet(
            [
                ["A100", "", "", 98, "bin mismatch"],
                ["A200", "", "", None, ""],
                ["A300", "", "", 0, ""],
            ]
        ),
    )
    response = confirm_sheet(client, frozen)
    assert response.status_code == 302
    assert line(frozen, "A100").physical_qty == D(98)
    assert line(frozen, "A200").physical_qty == D(4)  # blank never overwrites
    assert line(frozen, "A300").count_status == S.OK
    texts = list(line(frozen, "A100").remarks.values_list("text", flat=True))
    assert texts == ["old note", "bin mismatch"]
    assert ActivityLog.objects.filter(action="count (sheet)").count() == 2
    assert CountUpload.objects.get().rows == []


def test_sheet_changing_a_count_needs_the_tick(frozen, anil):
    client = client_for(anil)
    count(client, frozen, "A100", "90")
    upload_sheet(client, frozen, sheet([["A100", "", "", 95, ""]]))
    response = confirm_sheet(client, frozen, expected=1)
    assert response.status_code == 200
    assert line(frozen, "A100").physical_qty == D(90)
    confirm_sheet(client, frozen, accept=True, expected=1)
    assert line(frozen, "A100").physical_qty == D(95)


def test_sheet_refuses_changes_made_after_preview(frozen, anil, sita):
    client = client_for(anil)
    upload_sheet(client, frozen, sheet([["A100", "", "", 95, ""], ["A200", "", "", 7, ""]]))
    count(client_for(sita), frozen, "A100", "90")  # counted after Anil's preview (0 changes)
    response = confirm_sheet(client, frozen, accept=True, expected=0)
    assert "Counts changed since the preview" in response.content.decode()
    assert line(frozen, "A100").physical_qty == D(90)
    assert line(frozen, "A200").physical_qty is None


def test_sheet_keeps_formula_like_text_as_text(frozen, anil):
    a100 = line(frozen, "A100")
    a100.description = "=1+1"
    a100.save()
    response = client_for(anil).get(reverse("audits:count_sheet", args=[frozen.pk]))
    cell = load_workbook(io.BytesIO(response.content)).active["B2"]
    assert cell.value == "=1+1" and cell.data_type == "s"


def test_sheet_remark_keeps_line_breaks(frozen, anil):
    client = client_for(anil)
    upload_sheet(client, frozen, sheet([["A100", "", "", None, "Bin 4\nDamaged 2"]]))
    confirm_sheet(client, frozen)
    assert Remark.objects.get().text == "Bin 4\nDamaged 2"


def test_sheet_csv_with_only_needed_columns(frozen, anil):
    client = client_for(anil)
    file = SimpleUploadedFile("count.csv", b"Item No,Physical Qty\nA100,100\n")
    upload_sheet(client, frozen, file)
    confirm_sheet(client, frozen)
    assert line(frozen, "A100").count_status == S.OK


def test_sheet_blocked_after_checking_done(frozen, anil):
    client = client_for(anil)
    upload_sheet(client, frozen, sheet([["A100", "", "", 95, ""]]))
    frozen.stage = Audit.Stage.CHECKING_DONE
    frozen.save()
    response = confirm_sheet(client, frozen)
    assert response.status_code == 403
    assert upload_sheet(client, frozen, sheet([["A100", "", "", 95, ""]])).status_code == 403
    assert line(frozen, "A100").physical_qty is None


# ---------- remarks ----------


def add_remark(client, audit_obj, text, line_obj=None):
    data = {"text": text}
    if line_obj:
        data["line"] = line_obj.pk
    return client.post(
        reverse("audits:remark_panel", args=[audit_obj.pk]), data, HTTP_HX_REQUEST="true"
    )


def test_line_and_general_remarks(frozen, anil):
    client = client_for(anil)
    add_remark(client, frozen, "bin mismatch", line(frozen, "A100"))
    add_remark(client, frozen, "store was locked till 11am")
    assert Remark.objects.filter(line__isnull=True).count() == 1
    assert line(frozen, "A100").remarks.get().created_by == anil
    assert ActivityLog.objects.filter(action="add remark").count() == 2
    assert "Type a remark first" in add_remark(client, frozen, "   ").content.decode()


def test_only_writer_edits_or_deletes(frozen, anil, sita):
    add_remark(client_for(anil), frozen, "first", line(frozen, "A100"))
    remark = Remark.objects.get()
    edit = reverse("audits:remark_edit", args=[remark.pk])
    delete = reverse("audits:remark_delete", args=[remark.pk])
    assert client_for(sita).post(edit, {"text": "hacked"}).status_code == 403
    assert client_for(sita).post(delete).status_code == 403
    client_for(anil).post(edit, {"text": "second"}, HTTP_HX_REQUEST="true")
    remark.refresh_from_db()
    assert remark.text == "second" and remark.edited_at
    client_for(anil).post(delete, HTTP_HX_REQUEST="true")
    remark.refresh_from_db()
    assert remark.is_deleted  # soft delete: the row stays
    assert ActivityLog.objects.filter(action__in=["edit remark", "delete remark"]).count() == 2


def test_remarks_open_after_checking_done_closed_when_locked(frozen, anil):
    client = client_for(anil)
    add_remark(client, frozen, "early")
    frozen.stage = Audit.Stage.CHECKING_DONE
    frozen.save()
    add_remark(client, frozen, "after checking")
    remark = Remark.objects.get(text="early")
    client.post(reverse("audits:remark_edit", args=[remark.pk]), {"text": "edited"})
    assert Remark.objects.filter(text="edited").exists()
    frozen.stage = Audit.Stage.LOCKED
    frozen.save()
    assert add_remark(client, frozen, "too late").status_code == 403
    assert client.post(reverse("audits:remark_delete", args=[remark.pk])).status_code == 403


# ---------- Checking Done ----------


def test_checking_done_blocked_with_nothing_counted(frozen, anil):
    client = client_for(anil)
    url = reverse("audits:checking_done", args=[frozen.pk])
    assert "Nothing is counted yet" in client.get(url).content.decode()
    client.post(url, {"confirm": "on"})
    frozen.refresh_from_db()
    assert frozen.stage == Audit.Stage.ERP_FROZEN


def test_checking_done_raises_queries_only_for_flagged(frozen, anil):
    client = client_for(anil)
    count(client, frozen, "A100", "90")  # Shortage
    count(client, frozen, "A200", "6")  # OK (within 2)
    # A300 not counted
    url = reverse("audits:checking_done", args=[frozen.pk])
    client.post(url, {})  # no tick
    frozen.refresh_from_db()
    assert frozen.stage == Audit.Stage.ERP_FROZEN
    client.post(url, {"confirm": "on"})
    frozen.refresh_from_db()
    assert frozen.stage == Audit.Stage.CHECKING_DONE
    assert frozen.checking_done_by == anil and frozen.checking_done_at
    query = Query.objects.get()
    assert query.line.item_code == "A100" and query.status == Query.Status.OPEN
    assert ActivityLog.objects.filter(action="checking done").exists()
    # counts are fixed now
    assert count(client, frozen, "A200", "1", htmx=False).status_code == 403
    assert client.post(url, {"confirm": "on"}).status_code == 403
    assert Query.objects.count() == 1


# ---------- Admin cancel ----------


def test_admin_cancel_refused_once_counted(frozen, anil, admin_client):
    url = reverse("audits:audit_cancel", args=[frozen.pk])
    assert admin_client.get(url).status_code == 200
    count(client_for(anil), frozen, "A100", "5")
    assert admin_client.get(url).status_code == 403
    page = admin_client.get(reverse("audits:audit_detail", args=[frozen.pk])).content.decode()
    assert "can no longer be cancelled" in page
    admin_client.post(url, {"reason": "wrong branch"})
    frozen.refresh_from_db()
    assert frozen.stage == Audit.Stage.ERP_FROZEN
