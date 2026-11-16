# ruff: noqa: F811, F401  (pytest fixtures imported from other test files)
"""Phase 9: dashboard Item filter, one Trend tab (Variance merged in), repeat items grid."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from audits import dashboards
from audits.models import Action, Audit, Item
from core.models import Branch

from .conftest import client_for
from .test_dashboards_backup import dash, filters_for, make_audit, north, south, viewer

TODAY = timezone.localdate()


def ago(days: int):
    return TODAY - timedelta(days=days)


def item(code: str) -> Item:
    return Item.objects.get(code=code)


def csv_of(response) -> list[list[str]]:
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    return [line.split(",") for line in text.splitlines()]


def search(client, text: str):
    return client.get(reverse("audits:dashboard_items"), {"q": text}).content.decode()


# ---------- 1. Item filter ----------


def test_item_search_by_code_and_description(admin_client, company_a, company_b, branches):
    Item.objects.create(company=company_a, code="ITM-007", description="Soap 100g")
    Item.objects.create(company=company_a, code="RICE-5", description="Basmati rice")
    Item.objects.create(company=company_b, code="SOAP-B", description="Soap other company")
    by_code = search(admin_client, "itm-0")
    assert "ITM-007 – Soap 100g" in by_code and "RICE-5" not in by_code
    by_text = search(admin_client, "soap")
    assert "ITM-007" in by_text and "SOAP-B" not in by_text  # never another company's items
    assert "No item matches" in search(admin_client, "zzz")


def test_item_search_caps_results(admin_client, company_a, branches):
    for number in range(25):
        Item.objects.create(company=company_a, code=f"P{number:02d}")
    body = search(admin_client, "P")
    assert body.count("data-id=") == 20 and "type more letters" in body


def test_item_search_needs_view_reports(company_a, branches):
    from .conftest import make_user

    nobody = make_user("nobody")
    assert client_for(nobody).get(reverse("audits:dashboard_items")).status_code == 403


def test_item_filter_reads_only_this_company(branches, company_b):
    mine = Item.objects.create(company=branches[0].company, code="A1")
    theirs = Item.objects.create(company=company_b, code="A1")
    filters = filters_for(branches, item=[str(mine.pk), str(theirs.pk), "abc"])
    assert filters.items == [mine]
    assert any("not found in this company" in error for error in filters.errors)
    assert ("item", str(mine.pk)) in filters.as_params()


def test_item_filter_kept_in_tab_links(admin_client, admin_user, north):
    make_audit(north, "N-1", TODAY, admin_user, lines=[("A1", 10, 0, 1)])
    picked = item("A1")
    page = dash(admin_client, "status", item=str(picked.pk)).content.decode()
    # Status: picker shown as off with a tooltip, but the pick is kept for the other tabs.
    assert "Not used on this tab" in page
    assert f'name="item" value="{picked.pk}"' in page
    assert f"item={picked.pk}" in page  # tab links carry it
    trend = dash(admin_client, "trend", item=str(picked.pk)).content.decode()
    assert 'x-data="itemPicker' in trend and "1 picked" in trend


def test_queries_tab_narrows_to_picked_items(admin_client, admin_user, north):
    audit_obj = make_audit(
        north, "Aug", TODAY, admin_user, lines=[("A1", 10, 0, 1), ("B1", 10, 0, 1)]
    )
    past = ago(4)
    for query in audit_obj.queries.all():
        Action.objects.create(
            query=query,
            description=f"recount {query.item.code}",
            due_date=past,
            owner=admin_user,
            created_by=admin_user,
        )
    filters = filters_for([north], today=TODAY, item=str(item("A1").pk))
    assert dashboards.open_queries(filters).count() == 1
    assert [a.item_code for a in dashboards.overdue_actions(filters, today=TODAY)] == ["A1"]
    page = dash(admin_client, "queries", item=str(item("A1").pk)).content.decode()
    assert "recount A1" in page and "recount B1" not in page


# ---------- 2. One Trend tab ----------


def test_variance_url_redirects_to_trend(admin_client, north):
    response = admin_client.get(
        reverse("audits:dashboard_tab", args=["variance"]), {"branch": north.pk}
    )
    assert response.status_code == 302
    assert response.url == reverse("audits:dashboard_tab", args=["trend"]) + f"?branch={north.pk}"


def test_tabs_have_no_variance_and_trend_has_no_own_filters(admin_client, north):
    page = dash(admin_client, "trend").content.decode()
    assert "/dashboard/variance/" not in page
    assert 'name="trend_branch"' not in page and 'placeholder="e.g. ITM-007"' not in page


def test_trend_one_branch_per_audit_many_branches_per_month(
    admin_client, admin_user, branches, north, south
):
    make_audit(north, "N-a", ago(40), admin_user, lines=[("A1", 10, 20, 1)])
    make_audit(north, "N-b", TODAY, admin_user, lines=[("A1", 10, 7, 1)])
    make_audit(south, "S-a", TODAY, admin_user, lines=[("A1", 10, 0, 1)])
    many = csv_of(dash(admin_client, "trend", export="csv"))
    assert many[0] == ["Month", "Branch", "Excess value", "Shortage value", "Net variance"]
    # Same numbers as the old Variance tab.
    expected = dashboards.variance_rows(filters_for(branches, today=TODAY))
    assert [row[1] for row in many[1:]] == [row["branch"] for row in expected]
    one = csv_of(dash(admin_client, "trend", export="csv", branch=str(north.pk)))
    assert one[0][0] == "Audit" and [row[0] for row in one[1:]] == ["N1 N-a", "N1 N-b"]


def test_trend_items_narrow_the_sums(admin_user, branches, north):
    make_audit(north, "N-a", TODAY, admin_user, lines=[("A1", 10, 20, 1), ("B1", 10, 0, 2)])
    all_items = dashboards.variance_rows(filters_for(branches, today=TODAY))
    only_a1 = dashboards.variance_rows(filters_for(branches, today=TODAY, item=str(item("A1").pk)))
    assert all_items[0]["net"] == Decimal(-10)  # +10 excess, -20 shortage
    assert only_a1[0]["net"] == Decimal(10)


def test_trend_drilldown_respects_item_filter(admin_client, admin_user, north):
    audit_obj = make_audit(
        north, "N-a", TODAY, admin_user, lines=[("A1", 10, 20, 1), ("B1", 10, 0, 1)]
    )
    body = admin_client.get(
        reverse("audits:dashboard_detail", args=["trend"]),
        {"audit": audit_obj.pk, "item": item("A1").pk},
    ).content.decode()
    assert "A1" in body and "B1" not in body


# ---------- 3. Repeat items grid ----------


@pytest.fixture
def repeat_runs(admin_user, north, south):
    """North: A1 short, short, OK, short, short (+ B1 excess then short = mixed).
    South: one audit only, A1 short."""
    plan = [
        ("N-1", 150, [("A1", 10, 0, 1)]),
        ("N-2", 120, [("A1", 10, 8, 1), ("B1", 10, 14, 1)]),
        ("N-3", 90, [("A1", 10, 10, 1), ("B1", 10, 5, 1)]),
        ("N-4", 60, [("A1", 10, 0, 1), ("C1", 10, None, 1)]),
        ("N-5", 30, [("A1", 10, 5, 1), ("C1", 10, None, 1)]),
        ("N-6", 15, [("B1", 10, 10, 1)]),
        ("N-7", 0, [("A1", 10, 7, 1), ("B1", 10, None, 1)]),
    ]
    for title, days, lines in plan:
        make_audit(north, title, ago(days), admin_user, lines=lines)
    make_audit(south, "S-1", TODAY, admin_user, lines=[("A1", 10, 0, 1)])


def test_repeat_grid_cells(branches, repeat_runs):
    rows = dashboards.repeat_items(filters_for(branches, today=TODAY))
    row = next(r for r in rows if r["item_code"] == "A1" and r["branch_code"] == "N1")
    # Last 6 North audits: N-2 .. N-7, oldest first.
    assert [c["audit_title"] for c in row["cells"]] == ["N-2", "N-3", "N-4", "N-5", "N-6", "N-7"]
    assert [c["state"] for c in row["cells"]] == [
        "short",
        "ok",
        "short",
        "short",
        "missing",
        "short",
    ]
    assert row["cells"][0]["qty"] == Decimal(-2)
    assert row["cells"][0]["erp_qty"] == Decimal(10) and row["cells"][0]["physical_qty"] == Decimal(
        8
    )
    assert row["streak"] == 2 and row["direction"] == "short"
    mixed = next(r for r in rows if r["item_code"] == "B1")
    assert mixed["direction"] == "mixed"
    assert mixed["cells"][-1]["state"] == "not_counted"
    assert not any(r["branch_code"] == "S1" for r in rows)  # one audit: no streak


def test_repeat_grid_pads_short_branch(admin_user, branches, north):
    make_audit(north, "N-1", ago(40), admin_user, lines=[("A1", 10, 0, 1)])
    make_audit(north, "N-2", TODAY, admin_user, lines=[("A1", 10, 0, 1)])
    row = dashboards.repeat_items(filters_for(branches, today=TODAY))[0]
    assert [c["state"] for c in row["cells"]] == ["none"] * 4 + ["short", "short"]


def test_repeat_grid_page_colours_and_headers(admin_client, north, repeat_runs):
    page = dash(admin_client, "repeat").content.decode()
    assert '<b class="text-error">-2</b>' in page  # shortage qty in red
    assert '<b class="text-info">+4</b>' in page  # excess qty in blue
    assert 'aria-label="OK"' in page and 'aria-label="Not counted"' in page
    assert "6th last" in page and "Latest" in page  # several Branches: relative headings
    assert "Book 10, Counted 8 · Short 2" in page  # hover text
    one = dash(admin_client, "repeat", branch=str(north.pk)).content.decode()
    assert f"{TODAY:%d %b %y}" in one and "6th last" not in one  # one Branch: real dates


def test_repeat_min_streak_and_sort(admin_client, admin_user, north):
    for number, days in enumerate((90, 60, 30, 0)):
        lines = [("LONG", 10, 0, 1)] + ([("SHORT", 10, 0, 1)] if number >= 2 else [])
        make_audit(north, f"N-{number}", ago(days), admin_user, lines=lines)
    rows = csv_of(dash(admin_client, "repeat", export="csv"))
    assert [r[1] for r in rows[1:]] == ["LONG", "SHORT"]  # longest streak first
    assert rows[1][9] == "4 short"
    only_long = csv_of(dash(admin_client, "repeat", export="csv", streak_min="3"))
    assert [r[1] for r in only_long[1:]] == ["LONG"]
    page = dash(admin_client, "repeat", streak_min="3").content.decode()
    assert "Min streak:" in page and 'aria-current="true">3</a>' in page


def test_repeat_csv_has_cell_columns(admin_client, repeat_runs):
    rows = csv_of(dash(admin_client, "repeat", export="csv"))
    assert rows[0][3:9] == ["6th last", "5th last", "4th last", "3rd last", "2nd last", "Latest"]
    a1 = next(r for r in rows if r[0] == "N1" and r[1] == "A1")
    assert a1[3:9] == ["-2.000", "OK", "-10.000", "-5.000", "Not in audit", "-3.000"]


def test_repeat_table_redraw_sends_buttons_out_of_band(admin_client, repeat_runs):
    response = admin_client.get(
        reverse("audits:dashboard_tab", args=["repeat"]),
        {"q": "A1"},
        headers={"HX-Request": "true", "HX-Target": "repeat-table"},
    )
    body = response.content.decode()
    assert 'id="repeat-min-streak"' in body and 'hx-swap-oob="true"' in body
    assert "Dashboard filters" not in body  # only the table, not the whole dashboard


def test_repeat_row_panel_lists_every_audit(admin_client, north, repeat_runs):
    body = admin_client.get(
        reverse("audits:dashboard_detail", args=["repeat"]),
        {"row_branch": north.pk, "row_item": item("A1").pk},
    ).content.decode()
    titles = ["N-7", "N-5", "N-4", "N-3", "N-2", "N-1"]  # newest first, OK one too
    positions = [body.index(f">{title}<") for title in titles]
    assert positions == sorted(positions)
    assert "Counted qty" in body and "/queries/" in body


def test_repeat_row_panel_refuses_hidden_branch_and_other_item(
    viewer, admin_user, south, company_b, repeat_runs
):
    client = client_for(viewer)  # View reports in North only
    response = client.get(
        reverse("audits:dashboard_detail", args=["repeat"]),
        {"row_branch": south.pk, "row_item": item("A1").pk},
    )
    assert (
        response.status_code == 400 and "not in your dashboard filters" in response.content.decode()
    )
    other = Item.objects.create(company=company_b, code="A1")
    north_id = Branch.objects.get(code="N1").pk
    response = client.get(
        reverse("audits:dashboard_detail", args=["repeat"]),
        {"row_branch": north_id, "row_item": other.pk},
    )
    assert response.status_code == 400 and "not in this company" in response.content.decode()


def test_status_tab_ignores_item_filter(admin_user, branches, north):
    make_audit(north, "N-1", TODAY, admin_user, lines=[("A1", 10, 0, 1)])
    make_audit(north, "N-2", TODAY, admin_user, lines=[("B1", 10, 0, 1)])
    filters = filters_for(branches, today=TODAY, item=str(item("A1").pk))
    assert dashboards.status_rows(filters).count() == Audit.objects.count() == 2
