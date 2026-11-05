import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from accounts.models import BranchRight, Membership
from accounts.rights import Role
from audits.models import Audit, AuditLine, ErpUpload, Item
from core.models import ActivityLog

from .conftest import client_for, make_user

HEADINGS = ["Item No", "Description", "UoM", "ERP Qty", "Rate p.u."]
GOOD_ROWS = [
    ["A100", "Steel bolt 10mm", "PCS", 100, 2.5],
    ["A200", "Paint 1L", "LTR", 5, 120],
]


@pytest.fixture(autouse=True)
def media_in_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def xlsx_file(rows, headings=HEADINGS, name="erp.xlsx"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headings)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(name, buffer.getvalue())


def csv_file(text, name="erp.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"))


def grant(user, branch, company, role=Role.USER, **rights):
    Membership.objects.get_or_create(user=user, company=company, defaults={"role": role})
    BranchRight.objects.update_or_create(user=user, branch=branch, defaults=rights)


@pytest.fixture
def priya(branches, company_a):
    """User with upload right in North only."""
    user = make_user("priya")
    grant(user, branches[0], company_a, upload_erp=True)
    return user


@pytest.fixture
def manager(branches, company_a):
    user = make_user("mohan")
    grant(user, branches[0], company_a, role=Role.MANAGER, set_tolerance_freeze=True)
    return user


@pytest.fixture
def audit(priya, branches):
    return Audit.objects.create(
        branch=branches[0],
        title="Aug26",
        audit_date=date(2026, 8, 30),
        stock_as_on=date(2026, 8, 29),
        created_by=priya,
    )


def upload(client, audit, file):
    return client.post(reverse("audits:erp_upload", args=[audit.pk]), {"file": file})


def upload_and_confirm(client, audit, file, accept=False):
    response = upload(client, audit, file)
    pending = audit.uploads.filter(status=ErpUpload.Status.PENDING).first()
    assert pending, response
    data = {"accept_warnings": "on"} if accept else {}
    return client.post(reverse("audits:upload_preview", args=[pending.pk]), data)


# ---------- Template ----------


def test_template_has_the_five_headings(priya):
    response = client_for(priya).get(reverse("audits:erp_template"))
    sheet = load_workbook(io.BytesIO(response.content)).active
    assert [cell.value for cell in sheet[1]] == HEADINGS


# ---------- Create / edit ----------


def test_create_audit(priya, branches):
    client = client_for(priya)
    response = client.post(
        reverse("audits:audit_create"),
        {
            "branch": branches[0].pk,
            "title": "Aug26",
            "audit_date": "2026-08-30",
            "audit_type": "scheduled",
            "stock_as_on": "2026-08-29",
        },
    )
    audit = Audit.objects.get()
    assert response.url == reverse("audits:audit_detail", args=[audit.pk])
    assert audit.stage == Audit.Stage.ERP_DRAFT
    assert ActivityLog.objects.filter(action="create", record_type="Audit").exists()


def test_stock_date_after_audit_date_is_rejected(priya, branches):
    response = client_for(priya).post(
        reverse("audits:audit_create"),
        {
            "branch": branches[0].pk,
            "title": "X",
            "audit_date": "2026-08-30",
            "audit_type": "scheduled",
            "stock_as_on": "2026-08-31",
        },
    )
    assert "must be on or before the audit date" in response.content.decode()
    assert not Audit.objects.exists()


def test_title_unique_in_branch_but_same_date_allowed(priya, branches, audit):
    client = client_for(priya)
    data = {
        "branch": branches[0].pk,
        "audit_date": "2026-08-30",
        "audit_type": "surprise",
        "stock_as_on": "2026-08-30",
    }
    response = client.post(reverse("audits:audit_create"), {**data, "title": "aug26"})
    assert "already has an audit with this title" in response.content.decode()
    client.post(reverse("audits:audit_create"), {**data, "title": "Aug26-B"})
    assert Audit.objects.filter(branch=branches[0], audit_date=date(2026, 8, 30)).count() == 2


def test_cancelled_title_can_be_reused(admin_user, audit):
    audit.stage = Audit.Stage.CANCELLED
    audit.save()
    Audit.objects.create(
        branch=audit.branch,
        title="Aug26",
        audit_date=audit.audit_date,
        stock_as_on=audit.stock_as_on,
        created_by=admin_user,
    )


def test_user_without_upload_right_cannot_create(manager):
    response = client_for(manager).get(reverse("audits:audit_create"))
    assert response.status_code == 403


def test_other_branch_user_cannot_see_audit(audit, branches, company_a):
    outsider = make_user("sam")
    grant(outsider, branches[1], company_a, upload_erp=True)
    client = client_for(outsider)
    assert client.get(reverse("audits:audit_detail", args=[audit.pk])).status_code == 404
    assert audit.title not in client.get(reverse("audits:audit_list")).content.decode()


def test_creator_edits_draft_and_change_is_logged(priya, audit):
    client_for(priya).post(
        reverse("audits:audit_edit", args=[audit.pk]),
        {
            "title": "Aug26 main",
            "audit_date": "2026-08-30",
            "audit_type": "surprise",
            "stock_as_on": "2026-08-29",
        },
    )
    audit.refresh_from_db()
    assert audit.title == "Aug26 main"
    assert audit.audit_type == "surprise"
    assert ActivityLog.objects.filter(action="update", record_type="Audit").exists()


# ---------- Upload ----------


def test_upload_with_errors_is_blocked_and_nothing_saved(priya, audit):
    client = client_for(priya)
    rows = [
        ["A100", "Bolt", "PCS", "abc", 2],
        ["A100", "Bolt", "PCS", 5, 2],
        ["", "No code", "PCS", 1, 1],
        ["A300", "Nut", "", 1, 1],
    ]
    response = upload(client, audit, xlsx_file(rows))
    text = response.content.decode()
    assert "ERP Qty “abc” is not a number." in text
    assert "is repeated (first on row 2)" in text
    assert "Item No is missing." in text
    assert "UoM is missing." in text
    assert "Nothing was saved" in text
    assert "Confirm and save" not in text
    assert not ErpUpload.objects.exists()
    assert not AuditLine.objects.exists()
    assert not Item.objects.exists()


def test_preview_saves_nothing(priya, audit):
    upload(client_for(priya), audit, xlsx_file(GOOD_ROWS))
    assert not AuditLine.objects.exists()
    assert not Item.objects.exists()
    assert audit.uploads.get().status == ErpUpload.Status.PENDING


def test_confirm_saves_lines_items_and_totals(priya, audit):
    upload_and_confirm(client_for(priya), audit, xlsx_file(GOOD_ROWS))
    lines = {line.item_code: line for line in audit.lines.all()}
    assert lines["A100"].erp_value == Decimal("250.00")
    assert lines["A200"].erp_value == Decimal("600.00")
    assert set(Item.objects.values_list("code", "is_new")) == {("A100", True), ("A200", True)}
    used = audit.uploads.get()
    assert (used.status, used.line_count, used.total_value) == ("used", 2, Decimal("850.00"))
    assert ActivityLog.objects.filter(action="upload ERP file").exists()


def test_negative_qty_needs_the_tick(priya, audit):
    client = client_for(priya)
    rows = [*GOOD_ROWS, ["A300", "Nut", "PCS", -3, 1]]
    response = upload_and_confirm(client, audit, xlsx_file(rows))
    assert "checked the 1 warning line" in response.content.decode()
    assert not AuditLine.objects.exists()
    pending = audit.uploads.get()
    client.post(reverse("audits:upload_preview", args=[pending.pk]), {"accept_warnings": "on"})
    assert audit.lines.count() == 3


def test_replace_draft_swaps_lines_and_keeps_old_items(priya, audit):
    client = client_for(priya)
    upload_and_confirm(client, audit, xlsx_file(GOOD_ROWS))
    upload_and_confirm(client, audit, xlsx_file([["B1", "New thing", "KG", 1, 1]]))
    assert list(audit.lines.values_list("item_code", flat=True)) == ["B1"]
    assert Item.objects.count() == 3
    assert sorted(audit.uploads.values_list("status", flat=True)) == ["replaced", "used"]
    assert ActivityLog.objects.filter(action="replace ERP file").exists()


def test_line_keeps_file_description_and_master_unchanged(priya, audit, company_a):
    Item.objects.create(company=company_a, code="A100", description="Bolt 10")
    upload_and_confirm(client_for(priya), audit, xlsx_file(GOOD_ROWS))
    assert audit.lines.get(item_code="A100").description == "Steel bolt 10mm"
    assert Item.objects.get(code="A100").description == "Bolt 10"


def test_csv_with_any_heading_order_and_case(priya, audit):
    text = "rate p.u.;ITEM NO;erp qty;uom;description\n2,5;A100;10;PCS;Bolt\n"
    text = text.replace("2,5", "2.5")
    upload_and_confirm(client_for(priya), audit, csv_file(text))
    line = audit.lines.get()
    assert (line.item_code, line.erp_qty, line.rate) == ("A100", Decimal(10), Decimal("2.5"))


def test_missing_and_unknown_columns(priya, audit):
    client = client_for(priya)
    response = upload(client, audit, xlsx_file([], headings=["Item No", "Qty"]))
    page = response.content.decode()
    assert "Column “Qty” is not in the template" in page
    assert "Column “ERP Qty” not found." in page
    assert not ErpUpload.objects.exists()


def test_row_and_size_limits(priya, audit, settings):
    client = client_for(priya)
    settings.ERP_UPLOAD_MAX_ROWS = 2
    rows = [*GOOD_ROWS, ["A300", "Nut", "PCS", 1, 1]]
    page = upload(client, audit, xlsx_file(rows)).content.decode()
    assert "more than 2 lines" in page
    settings.ERP_UPLOAD_MAX_BYTES = 10
    page = upload(client, audit, xlsx_file(GOOD_ROWS)).content.decode()
    assert "File is too big" in page


def test_wrong_file_type(priya, audit):
    client = client_for(priya)
    response = upload(client, audit, SimpleUploadedFile("erp.pdf", b"%PDF"))
    assert "Only .xlsx (Excel) or .csv" in response.content.decode()
    assert not ErpUpload.objects.exists()


def test_user_without_upload_right_cannot_upload(manager, audit):
    response = upload(client_for(manager), audit, xlsx_file(GOOD_ROWS))
    assert response.status_code == 403


# ---------- Freeze ----------


def test_freeze_sets_tolerance_and_blocks_changes(priya, manager, audit):
    upload_and_confirm(client_for(priya), audit, xlsx_file(GOOD_ROWS))
    client = client_for(manager)
    url = reverse("audits:audit_freeze", args=[audit.pk])
    response = client.post(url, {"qty_tolerance": "2", "pct_tolerance": "5"})
    assert "Tick the box" in response.content.decode()
    client.post(url, {"qty_tolerance": "2", "pct_tolerance": "5", "confirm": "on"})
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.ERP_FROZEN
    assert (audit.qty_tolerance, audit.pct_tolerance) == (Decimal(2), Decimal(5))
    assert ActivityLog.objects.filter(action="freeze ERP stock").exists()
    # No more uploads, edits or second freeze.
    assert upload(client_for(priya), audit, xlsx_file(GOOD_ROWS)).status_code == 403
    assert client.get(url).status_code == 403
    edit = client_for(priya).get(reverse("audits:audit_edit", args=[audit.pk]))
    assert edit.status_code == 403


def test_pending_upload_cannot_be_confirmed_after_freeze(priya, manager, audit):
    client = client_for(priya)
    upload_and_confirm(client, audit, xlsx_file(GOOD_ROWS))
    upload(client, audit, xlsx_file([["B1", "x", "KG", 1, 1]]))
    pending = audit.uploads.get(status="pending")
    client_for(manager).post(
        reverse("audits:audit_freeze", args=[audit.pk]), {"qty_tolerance": "0", "confirm": "on"}
    )
    client.post(reverse("audits:upload_preview", args=[pending.pk]))
    assert audit.lines.count() == 2


def test_cannot_freeze_without_lines(manager, audit):
    response = client_for(manager).get(reverse("audits:audit_freeze", args=[audit.pk]))
    assert response.status_code == 302
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.ERP_DRAFT


# ---------- Cancel ----------


def test_cancel_needs_reason_and_is_logged(priya, audit):
    client = client_for(priya)
    url = reverse("audits:audit_cancel", args=[audit.pk])
    response = client.post(url, {"reason": ""})
    assert "A reason is required" in response.content.decode()
    client.post(url, {"reason": "Wrong Branch picked"})
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.CANCELLED
    assert ActivityLog.objects.filter(action="cancel", record_type="Audit").exists()


def test_creator_cannot_cancel_frozen_but_admin_can(priya, admin_user, audit):
    audit.stage = Audit.Stage.ERP_FROZEN
    audit.save()
    url = reverse("audits:audit_cancel", args=[audit.pk])
    assert client_for(priya).get(url).status_code == 403
    client_for(admin_user).post(url, {"reason": "Wrong ERP file frozen"})
    audit.refresh_from_db()
    assert audit.stage == Audit.Stage.CANCELLED


def test_other_user_cannot_cancel_someone_elses_draft(audit, branches, company_a):
    colleague = make_user("kiran")
    grant(colleague, branches[0], company_a, upload_erp=True)
    url = reverse("audits:audit_cancel", args=[audit.pk])
    assert client_for(colleague).get(url).status_code == 403


# ---------- Items ----------


def test_manager_reviews_new_item(manager, company_a):
    item = Item.objects.create(company=company_a, code="A100", is_new=True)
    client_for(manager).post(reverse("audits:item_review", args=[item.pk]))
    item.refresh_from_db()
    assert not item.is_new
    assert ActivityLog.objects.filter(action="review", record_type="Item").exists()


def test_plain_user_can_view_but_not_edit_items(priya, company_a):
    item = Item.objects.create(company=company_a, code="A100", description="Bolt")
    client = client_for(priya)
    assert "A100" in client.get(reverse("audits:item_list")).content.decode()
    assert client.post(reverse("audits:item_toggle", args=[item.pk])).status_code == 403
    assert client.get(reverse("audits:item_create")).status_code == 403


def test_item_code_unique_in_company_any_case(manager, company_a):
    Item.objects.create(company=company_a, code="A100")
    response = client_for(manager).post(
        reverse("audits:item_create"), {"code": "a100", "description": "x", "uom": "PCS"}
    )
    assert "already has an item with this code" in response.content.decode()


def test_items_and_audits_are_never_deleted(audit, company_a):
    item = Item.objects.create(company=company_a, code="A100")
    with pytest.raises(Exception, match="never deleted"):
        item.delete()
    with pytest.raises(Exception, match="never deleted"):
        audit.delete()


# ---------- Phase 2.1 ----------


def test_extra_decimals_are_a_warning_and_rounded(priya, audit):
    client = client_for(priya)
    rows = [["A100", "Bolt", "PCS", "12.34567", "1.235"]]
    response = upload(client, audit, xlsx_file(rows))
    pending = audit.uploads.get()
    text = client.get(reverse("audits:upload_preview", args=[pending.pk])).content.decode()
    assert response.status_code == 302
    assert "ERP Qty 12.34567 has more than 3 decimals; it will be saved as 12.346." in text
    assert "Rate p.u. 1.235 has more than 2 decimals; it will be saved as 1.24." in text
    client.post(reverse("audits:upload_preview", args=[pending.pk]))  # no tick
    assert not AuditLine.objects.exists()
    client.post(reverse("audits:upload_preview", args=[pending.pk]), {"accept_warnings": "on"})
    line = audit.lines.get()
    assert (line.erp_qty, line.rate) == (Decimal("12.346"), Decimal("1.24"))


def test_uploaded_file_is_not_stored(priya, audit, tmp_path):
    client = client_for(priya)
    upload(client, audit, xlsx_file(GOOD_ROWS))
    pending = audit.uploads.get()
    assert len(pending.rows) == 2 and pending.line_count == 2
    assert not any(tmp_path.rglob("*.xlsx"))
    client.post(reverse("audits:upload_preview", args=[pending.pk]))
    pending.refresh_from_db()
    assert pending.status == "used" and pending.rows == []
    # A confirmed upload can't be opened again; it sends the user back to the audit.
    response = client.get(reverse("audits:upload_preview", args=[pending.pk]))
    assert response.url == reverse("audits:audit_detail", args=[audit.pk])


def bulk_review(client, follow=False, **data):
    return client.post(reverse("audits:item_bulk_review"), data, follow=follow)


@pytest.fixture
def new_items(company_a, company_b):
    made = [
        Item.objects.create(company=company_a, code=code, description=text, is_new=True)
        for code, text in [("RM-1", "Raw steel"), ("RM-2", "Raw paint"), ("FG-1", "Finished bolt")]
    ]
    made.append(Item.objects.create(company=company_a, code="RM-3", is_new=False))
    made.append(Item.objects.create(company=company_b, code="RM-9", is_new=True))
    return made


def test_bulk_review_ticked_items(manager, new_items):
    rm1, _rm2, fg1, _rm3, other_company = new_items
    response = bulk_review(client_for(manager), ids=[rm1.pk, fg1.pk, other_company.pk])
    assert response.status_code == 302
    assert set(Item.objects.filter(is_new=True).values_list("code", flat=True)) == {"RM-2", "RM-9"}
    assert ActivityLog.objects.filter(action="review", record_type="Item").count() == 2


def test_bulk_review_all_matching_filter_with_exclusion(manager, new_items):
    _rm1, rm2, _fg1, _rm3, _other = new_items
    client = client_for(manager)
    listing = client.get(reverse("audits:item_list"), {"q": "rm"}, HTTP_HX_REQUEST="true")
    assert 'data-matching="2"' in listing.content.decode()  # RM-1, RM-2 (not reviewed RM-3)
    response = bulk_review(client, all_matching="1", q="rm", show="", exclude=[rm2.pk])
    assert set(Item.objects.filter(is_new=True).values_list("code", flat=True)) == {
        "RM-2",
        "FG-1",
        "RM-9",
    }
    assert ActivityLog.objects.filter(action="review").count() == 1
    assert response.url.endswith("?q=rm&show=")


def test_bulk_review_needs_the_right(priya, new_items):
    response = bulk_review(client_for(priya), ids=[new_items[0].pk])
    assert response.status_code == 403
    assert Item.objects.filter(is_new=True).count() == 4


def test_logout_is_one_post_and_lands_on_login(priya):
    client = client_for(priya)
    assert client.get(reverse("accounts:logout")).status_code == 405
    response = client.post(reverse("accounts:logout"), follow=True)
    assert response.redirect_chain[-1][0] == reverse("accounts:login")
    assert "You are logged out." in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_navbar_logout_row_is_the_submit_button(priya):
    text = client_for(priya).get(reverse("core:home")).content.decode()
    assert 'form="logout-form"' in text and 'id="logout-form"' in text


def test_too_long_uom_and_too_big_numbers_are_errors(priya, audit):
    rows = [
        ["A1", "x", "U" * 21, 1, 1],
        ["A2", "x", "PCS", 123456789012, 1],
        ["A3", "x", "PCS", 99999999999, 999999999],
    ]
    text = upload(client_for(priya), audit, xlsx_file(rows)).content.decode()
    assert "UoM is longer than 20 characters." in text
    assert "ERP Qty 123456789012 is too big." in text
    assert "ERP Qty × Rate p.u. is too big." in text
    assert not ErpUpload.objects.exists()


def test_rounding_and_negative_warnings_on_one_row(priya, audit):
    client = client_for(priya)
    upload(client, audit, xlsx_file([["A1", "x", "PCS", "-1.23456", 1]]))
    pending = audit.uploads.get()
    assert len(pending.rows[0][6]) == 2
    client.post(reverse("audits:upload_preview", args=[pending.pk]), {"accept_warnings": "on"})
    assert audit.lines.get().erp_qty == Decimal("-1.235")


def test_confirm_clears_other_pending_rows(priya, audit):
    client = client_for(priya)
    upload(client, audit, xlsx_file(GOOD_ROWS))
    upload_and_confirm(client, audit, xlsx_file(GOOD_ROWS))
    assert all(saved.rows == [] for saved in audit.uploads.all())


def test_bulk_review_ignores_bad_ids(manager, new_items):
    response = bulk_review(client_for(manager), ids=["²", "abc", str(new_items[0].pk)])
    assert response.status_code == 302
    assert Item.objects.filter(is_new=True).count() == 3


def test_bulk_review_refused_when_list_grew(manager, new_items):
    client = client_for(manager)
    response = bulk_review(client, all_matching="1", q="rm", show="", expected="1", follow=True)
    assert Item.objects.filter(is_new=True).count() == 4  # nothing changed
    assert "The list changed: 2 items now match, not 1." in response.content.decode()
