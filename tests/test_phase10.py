# ruff: noqa: F811, F401  (pytest fixtures imported from other test files)
"""Phase 10: pending actions (Branch summary, owner table, drill-down), query closing speed,
top 10 loss items and the KPI tiles."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from audits import dashboards
from audits.models import Action, Item, Query

from .conftest import make_user
from .test_dashboards_backup import dash, filters_for, make_audit, north, south

TODAY = timezone.localdate()


def ago(days: int):
    return TODAY - timedelta(days=days)


def csv_lines(response) -> list[str]:
    return b"".join(response.streaming_content).decode("utf-8-sig").splitlines()


@pytest.fixture
def actions(admin_user, north, south):
    """North: 5 actions — overdue Open, future Open, no-due Open, overdue Done, Verified.
    South: 1 future Open, owned by Ravi."""
    ravi = make_user("ravi")
    north_audit = make_audit(
        north, "N-Aug", TODAY, admin_user, lines=[("A1", 10, 0, 1), ("B1", 10, 0, 1)]
    )
    south_audit = make_audit(south, "S-Aug", TODAY, admin_user, lines=[("A1", 10, 0, 1)])
    on_a1 = north_audit.queries.get(item__code="A1")
    on_b1 = north_audit.queries.get(item__code="B1")
    mine = {"owner": admin_user, "created_by": admin_user}
    Action.objects.create(query=on_a1, description="late open", due_date=ago(3), **mine)
    Action.objects.create(query=on_a1, description="future open", due_date=ago(-5), **mine)
    Action.objects.create(query=on_b1, description="no due date", **mine)
    Action.objects.create(
        query=on_b1,
        description="late done",
        due_date=ago(2),
        status=Action.Status.DONE,
        **mine,
    )
    Action.objects.create(
        query=on_a1,
        description="all verified",
        due_date=ago(9),
        status=Action.Status.VERIFIED,
        **mine,
    )
    Action.objects.create(
        query=south_audit.queries.get(),
        description="south job",
        due_date=ago(-1),
        owner=ravi,
        created_by=admin_user,
    )
    return {"north": north, "south": south}


# ---------- pending actions ----------


def test_pending_counts_per_branch(actions, branches):
    rows = {row["branch"]: row for row in dashboards.pending_action_rows(filters_for(branches))}
    north = rows[actions["north"].code]
    # Verified left out; future and no-due-date actions are now counted too.
    assert (north["open"], north["waiting"], north["total"], north["overdue"]) == (3, 1, 4, 2)
    south = rows[actions["south"].code]
    assert (south["total"], south["overdue"]) == (1, 0)
    # Every picked Branch has a row, also with nothing pending.
    assert len(rows) == len(branches)


def test_pending_follows_item_filter(actions, branches):
    a1 = Item.objects.get(code="A1")
    filters = filters_for(branches, item=str(a1.pk))
    names = set(dashboards.pending_actions(filters).values_list("description", flat=True))
    assert names == {"late open", "future open", "south job"}


def test_owner_rows_most_overdue_first(actions, branches):
    rows = dashboards.owner_rows(filters_for(branches))
    assert [row["owner_name"] for row in rows] == ["Admin", "Ravi"]
    assert (rows[0]["total"], rows[0]["overdue"]) == (4, 2)


def test_queries_tab_shows_all_pending_and_drill_links(admin_client, actions):
    page = dash(admin_client, "queries").content.decode()
    for name in ("late open", "future open", "no due date", "late done", "south job"):
        assert name in page
    assert "all verified" not in page
    assert "Pending actions" in page and "Actions by owner" in page
    code = actions["north"].code
    assert f"branch_code={code}&amp;overdue=yes" in page  # Overdue number of North


def test_drill_down_redraws_only_the_table(admin_client, actions):
    url = reverse("audits:dashboard_tab", args=["queries"])
    code = actions["north"].code
    part = admin_client.get(
        url,
        {"branch_code": code, "overdue": "yes"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="overdue-table",
    ).content.decode()
    assert part.lstrip().startswith('<div id="overdue-table"')
    assert "late open" in part and "late done" in part
    assert "future open" not in part and "south job" not in part
    by_owner = admin_client.get(
        url, {"owner_name": "Ravi"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="overdue-table"
    ).content.decode()
    assert "south job" in by_owner and "late open" not in by_owner


def test_pending_csvs(admin_client, actions):
    url = reverse("audits:dashboard_tab", args=["queries"])
    summary = csv_lines(admin_client.get(url, {"export": "csv", "part": "summary"}))
    assert summary[0] == "Branch,Open,Waiting to verify,Total pending,Overdue"
    assert f"{actions['north'].code},3,1,4,2" in summary
    owners = csv_lines(admin_client.get(url, {"export": "csv", "part": "owners"}))
    assert owners[1] == "Admin,3,1,4,2"
    table = csv_lines(admin_client.get(url, {"export": "csv", "part": "actions"}))
    assert len(table) == 6  # header + 5 pending
    future = next(line for line in table if "future open" in line)
    assert future.endswith(",Not late,")  # no days late before the due date


# ---------- closing speed ----------


def test_closing_speed_average(admin_user, branches, north):
    audit_obj = make_audit(
        north, "N-Jul", ago(20), admin_user, lines=[("A1", 10, 0, 1), ("B1", 10, 0, 1)]
    )
    now = timezone.now()
    for query, days in zip(audit_obj.queries.order_by("item__code"), (4, 10)):
        Query.objects.filter(pk=query.pk).update(
            status=Query.Status.CLOSED,
            created_at=now - timedelta(days=days),
            closed_at=now,
        )
    rows = dashboards.closing_speed(filters_for(branches))
    assert rows == [{"branch": north.code, "closed": 2, "avg_days": 7.0, "max_days": 10}]


# ---------- top loss items ----------


def test_top_loss_items_order_and_limit(admin_user, branches, north, south):
    lines = [(f"I{n:02d}", 100, 0, n) for n in range(1, 13)]  # I12 loses most
    make_audit(north, "N-1", ago(10), admin_user, lines=lines)
    make_audit(south, "S-1", ago(10), admin_user, lines=[("I01", 100, 0, 50)])
    rows = dashboards.top_loss_items(filters_for(branches))
    codes = [row["item_code"] for row in rows]
    # I01 adds up both Branches: 100 + 5,000 short, so it beats I12 (1,200).
    assert len(rows) == 10 and codes[:3] == ["I01", "I12", "I11"]
    assert rows[0]["times"] == 2 and rows[0]["shortage"] == 5100
    assert "I02" not in codes and "I03" not in codes  # the 2 smallest drop off


def test_top_loss_on_trend_tab_with_detail(admin_client, admin_user, north, south):
    make_audit(north, "N-1", ago(10), admin_user, lines=[("A1", 10, 0, 5)])
    make_audit(south, "S-1", ago(10), admin_user, lines=[("A1", 10, 0, 5)])
    page = dash(admin_client, "trend").content.decode()
    assert "loss items" in page and "loss-detail-1" in page
    a1 = Item.objects.get(code="A1")
    detail = admin_client.get(
        reverse("audits:dashboard_detail", args=["loss"]), {"row_item": a1.pk}
    ).content.decode()
    assert north.code in detail and south.code in detail and detail.count("N-1") == 1
    csv = csv_lines(dash(admin_client, "trend", export="csv", part="toploss"))
    assert csv[0] == "Item No,Description,Times short,Shortage value" and csv[1].startswith("A1,")


# ---------- KPI tiles ----------


def test_kpis_match_tabs(actions, branches, admin_user, north):
    make_audit(north, "Draft", TODAY, admin_user, stage="erp_draft")
    numbers = dashboards.kpis(filters_for(branches))
    filters = filters_for(branches)
    assert numbers["audits"] == 3 and numbers["audits_done"] == 2
    assert numbers["open_queries"] == dashboards.open_queries(filters).count() == 3
    assert (numbers["pending_actions"], numbers["overdue_actions"]) == (5, 2)
    assert numbers["net_variance"] == -30  # three 10-unit shortages at rate 1


def test_kpi_tiles_on_page_not_in_table_redraw(admin_client, actions):
    page = dash(admin_client, "status").content.decode()
    assert "Pending actions" in page and "2 overdue" in page and "Net variance" in page
    part = admin_client.get(
        reverse("audits:dashboard_tab", args=["queries"]),
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="overdue-table",
    ).content.decode()
    assert "Net variance" not in part
