"""Phase 8: pilot feedback — filter pop-up layer, logo, phone menu, tab style, overdue and
repeat items on the shared table, compact audit page, PDF line remarks and links."""

import io
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from audits.models import Action, Attachment, AuditLine, Remark
from core.datatable import CHOICE, NUMBER, Column, RowsTable

from .conftest import client_for, make_user
from .test_audits import audit, manager, priya  # noqa: F401
from .test_counting import anil, count, frozen  # noqa: F401
from .test_dashboards_backup import dash, make_audit, north, south  # noqa: F401
from .test_lock_media_reports import (  # noqa: F401
    add_media,
    jpeg,
    lock,
    locker,
    media_user,
    mp4,
    pdf_text,
)
from .test_queries import boss, raised, replier  # noqa: F401


def png_bytes(size=(120, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (20, 60, 160)).save(out, "PNG")
    return out.getvalue()


def give_logo(company):
    company.logo.save("logo.png", ContentFile(png_bytes()), save=True)


# ---------- 1. filter pop-up of the 1st column ----------


def test_header_sticky_cell_sits_above_body_cells():
    css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert ".data-table thead .dt-sticky { z-index: 3; }" in css


# ---------- 2. logo and phone menu ----------


def test_top_bar_shows_company_logo(admin_client, company_a, north):  # noqa: F811
    page = admin_client.get(reverse("audits:dashboard")).content.decode()
    assert "company-logo" not in page  # no logo yet: nothing, not a broken image
    give_logo(company_a)
    page = admin_client.get(reverse("audits:dashboard")).content.decode()
    assert "company-logo" in page and company_a.logo.url in page


def test_phone_menu_has_same_links_as_top_bar(admin_client, north):  # noqa: F811
    page = admin_client.get(reverse("audits:item_list")).content.decode()
    assert 'id="nav-drawer"' in page and "Open the menu" in page
    for name in ("audits:dashboard", "audits:audit_list", "audits:query_list", "audits:item_list"):
        assert page.count(f'href="{reverse(name)}"') == 2, name  # top bar + side menu
    assert page.count(f'href="{reverse("accounts:user_list")}"') == 2  # Admin menu, both
    assert page.count('form="logout-form"') == 2
    # Two theme switches (bar + side menu) must not share one id.
    assert page.count('id="theme-toggle"') == 1 and 'id="theme-toggle-phone"' in page


def test_phone_menu_hides_admin_links_from_others(raised, boss):  # noqa: F811
    page = client_for(boss).get(reverse("audits:query_list")).content.decode()
    assert 'id="nav-drawer"' in page
    assert reverse("accounts:user_list") not in page


# ---------- 3. one tab style ----------


def test_dashboard_and_audit_tabs_share_style(admin_client, raised):  # noqa: F811
    assert "tabs app-tabs" in dash(admin_client, "status").content.decode()
    page = admin_client.get(reverse("audits:audit_detail", args=[raised.audit_id])).content.decode()
    assert "tabs app-tabs" in page and "tabs-lift" not in page


# ---------- 4. overdue actions ----------


def overdue_setup(admin_user, north):  # noqa: F811
    audit_obj = make_audit(
        north, "Aug26", timezone.localdate(), admin_user, lines=[("A1", 10, 0, 1)]
    )
    query = audit_obj.queries.get()
    common = {"query": query, "owner": admin_user, "created_by": admin_user}
    past = timezone.localdate() - timedelta(days=4)
    Action.objects.create(description="count rack again", due_date=past, **common)
    Action.objects.create(
        description="fixed in ERP", due_date=past, status=Action.Status.DONE, **common
    )
    return query


def test_overdue_marks_done_as_waiting_and_filters(admin_client, admin_user, north):  # noqa: F811
    overdue_setup(admin_user, north)
    page = dash(admin_client, "queries").content.decode()
    assert "count rack again" in page and "fixed in ERP" in page
    assert "Done – waiting to verify" in page
    only_open = dash(admin_client, "queries", status="open").content.decode()
    assert "count rack again" in only_open and "fixed in ERP" not in only_open


def test_overdue_table_click_and_csv(admin_client, admin_user, north):  # noqa: F811
    overdue_setup(admin_user, north)
    url = reverse("audits:dashboard_tab", args=["queries"])
    part = admin_client.get(
        url, {"sort": "-description"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="overdue-table"
    ).content.decode()
    assert part.lstrip().startswith('<div id="overdue-table"') and "<html" not in part
    csv_text = b"".join(
        admin_client.get(url, {"export": "csv", "part": "overdue"}).streaming_content
    ).decode("utf-8-sig")
    lines = csv_text.splitlines()
    assert lines[0] == "Branch,Audit,Item No,Action,Owner,Status,Due,Days late"
    assert len(lines) == 3 and lines[1].endswith(",4") and "Done" in csv_text
    # The ageing CSV (no part) is unchanged.
    ageing = b"".join(admin_client.get(url, {"export": "csv"}).streaming_content).decode()
    assert "Total open" in ageing


# ---------- 5. repeat items ----------


def test_repeat_items_on_table_grouped_by_branch(admin_client, admin_user, north, south):  # noqa: F811
    today = timezone.localdate()
    for branch in (south, north):
        make_audit(branch, "R1", today - timedelta(days=40), admin_user, lines=[("A1", 10, 0, 1)])
        make_audit(branch, "R2", today, admin_user, lines=[("A1", 10, 0, 1), ("B1", 10, 0, 1)])
    page = dash(admin_client, "repeat").content.decode()
    assert 'id="repeat-table"' in page and "Search item code or description" in page
    csv_text = b"".join(dash(admin_client, "repeat", export="csv").streaming_content).decode(
        "utf-8-sig"
    )
    rows = [line.split(",") for line in csv_text.splitlines()[1:]]
    assert [(r[0], r[1], r[3]) for r in rows] == [("N1", "A1", "2"), ("S1", "A1", "2")]
    assert rows[0][4] == "-20.00"
    only_south = b"".join(
        dash(admin_client, "repeat", export="csv", branch_code="S1").streaming_content
    ).decode("utf-8-sig")
    assert "N1" not in only_south and "S1,A1" in only_south


def test_repeat_items_empty(admin_client, north):  # noqa: F811
    assert "No item had Excess / Shortage" in dash(admin_client, "repeat").content.decode()


# ---------- 6. compact audit page ----------


def test_audit_page_top_is_compact(raised, locker):  # noqa: F811
    lock(locker, raised.audit)
    page = client_for(locker).get(reverse("audits:audit_detail", args=[raised.audit_id]))
    html = page.content.decode()
    assert 'class="stage-line"' in html and html.count('class="is-done"') == 4
    assert "Checking Done by" in html and "Locked by" in html
    assert 'role="alert" class="alert alert-soft alert-info' not in html  # no big banners
    assert html.count('class="mini-stat"') == 8  # 3 ERP boxes + 5 count boxes in one row
    assert 'id="count-stats"' in html


# ---------- 7. PDF ----------


def test_pdf_line_remarks_general_remarks_and_links(raised, media_user, locker, company_a):  # noqa: F811
    audit_obj = raised.audit
    line = raised.line
    Remark.objects.create(audit=audit_obj, line=line, text="Found 5 in rack B", created_by=locker)
    Remark.objects.create(audit=audit_obj, text="Store was tidy", created_by=locker)
    ok_line = audit_obj.lines.get(count_status=AuditLine.Status.OK)
    Remark.objects.create(audit=audit_obj, line=ok_line, text="Label faded", created_by=locker)
    add_media(media_user, "line", line.pk, jpeg("rack.jpg"))
    photo = Attachment.objects.get()
    client_for(media_user).post(
        reverse("audits:attachment_report", args=[photo.pk]), {"include": "on"}
    )
    give_logo(company_a)

    text = pdf_text(client_for(locker).get(reverse("audits:audit_pdf", args=[audit_obj.pk])))
    assert "Found 5 in rack B" in text and "Remark:" in text
    assert "General remarks \\(1\\)" in text and "Store was tidy" in text
    assert "Remarks on OK lines \\(1\\)" in text and "Label faded" in text
    assert "Photos \\(1\\)" in text and "Go to the line" in text
    # Jumps inside the PDF: line row → its photo, photo → line row (queries sit under their line).
    assert text.count("/Subtype /Link") == 2
    assert text.count("/Subtype /Image") == 2  # the photo + the company logo


def test_pdf_lists_other_files(raised, media_user, locker):  # noqa: F811
    add_media(media_user, "line", raised.line_id, mp4("walk.mp4"))
    text = pdf_text(client_for(locker).get(reverse("audits:audit_pdf", args=[raised.audit_id])))
    assert "Other files \\(1\\)" in text and "walk.mp4" in text and "Files \\(1\\)" in text


def test_pdf_without_logo_still_builds(raised, locker):  # noqa: F811
    text = pdf_text(client_for(locker).get(reverse("audits:audit_pdf", args=[raised.audit_id])))
    assert "Stock audit report" in text and "/Subtype /Image" not in text


# ---------- review fixes ----------


def test_repeat_item_opens_query_with_item_history(admin_client, admin_user, north):  # noqa: F811
    today = timezone.localdate()
    make_audit(north, "Jul26", today - timedelta(days=40), admin_user, lines=[("A1", 10, 0, 1)])
    latest = make_audit(north, "Aug26", today, admin_user, lines=[("A1", 10, 0, 1)])
    query = latest.queries.get()
    page = dash(admin_client, "repeat").content.decode()
    assert reverse("audits:query_detail", args=[query.pk]) in page
    detail = admin_client.get(reverse("audits:query_detail", args=[query.pk])).content.decode()
    assert "Item history" in detail and "Jul26" in detail and "Aug26" in detail


def test_overdue_owner_falls_back_to_username(admin_client, admin_user, north):  # noqa: F811
    query = overdue_setup(admin_user, north)
    nameless = make_user("nameless")
    nameless.full_name = ""
    nameless.save(update_fields=["full_name"])
    Action.objects.filter(query=query).update(owner=nameless)
    page = dash(admin_client, "queries").content.decode()
    assert "<td>nameless</td>" in page


def rows_table(params: dict):
    query = QueryDict(mutable=True)
    for key, value in params.items():
        query.setlist(key, value if isinstance(value, list) else [value])
    rows = [
        {"code": "B1", "kind": "x", "size": Decimal(5)},
        {"code": "a2", "kind": "", "size": None},
        {"code": "C3", "kind": "y", "size": Decimal(-2)},
    ]
    columns = [Column("code", "Code"), Column("kind", "Kind", type=CHOICE)]
    columns.append(Column("size", "Size", type=NUMBER))
    return RowsTable(query, rows, columns, table_id="t", search_fields=("code",))


def codes(table) -> list[str]:
    return [row["code"] for row in table.page]


def test_rows_table_filters_sorts_and_counts():
    assert codes(rows_table({})) == ["B1", "a2", "C3"]  # rows keep their own order
    assert codes(rows_table({"sort": "code"})) == ["a2", "B1", "C3"]  # A→Z ignoring case
    assert codes(rows_table({"sort": "-size"})) == ["B1", "C3", "a2"]  # empty cells last
    assert codes(rows_table({"size_min": "0"})) == ["B1"]
    assert codes(rows_table({"kind": "__blank__"})) == ["a2"]
    assert codes(rows_table({"q": "c"})) == ["C3"]
    table = rows_table({"kind": "x"})
    kind_cell = next(cell for cell in table.header if cell["column"].name == "kind")
    assert {o["value"]: o["count"] for o in kind_cell["options"]} == {
        "x": 1,
        "y": 1,
        "__blank__": 1,
    }
    bad = rows_table({"size_min": "lots"})
    assert codes(bad) == ["B1", "a2", "C3"] and "is not a number" in bad.errors[0]


def test_theme_switches_stay_in_step_and_drawer_resets():
    root = Path(settings.BASE_DIR)
    assert "theme-changed" in (root / "static/js/theme.js").read_text(encoding="utf-8")
    toggle = (root / "templates/partials/theme_toggle.html").read_text(encoding="utf-8")
    assert "@theme-changed.window" in toggle
    menus = (root / "static/js/menus.js").read_text(encoding="utf-8")
    assert "pageshow" in menus and "nav-drawer" in menus
