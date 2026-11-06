"""Home page “My pending items” tabs, and the dashboard drilldown (▸ expanders, ageing links)."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.urls import reverse
from django.utils import timezone

from audits import dashboard_views, dashboards
from audits.models import Action

from .conftest import client_for
from .test_audits import audit, manager, priya  # noqa: F401
from .test_counting import anil, count, frozen  # noqa: F401
from .test_dashboards_backup import filters_for, make_audit, north, south, viewer  # noqa: F401
from .test_queries import add_action, boss, doer, raised, replier, reply  # noqa: F401


def home(client, **params):
    return client.get(reverse("core:home"), params)


def htmx(client, target, **params):
    return client.get(reverse("core:home"), params, HTTP_HX_REQUEST="true", HTTP_HX_TARGET=target)


# ---------- My pending items ----------


def test_pending_tabs_show_counts_and_open_first_with_work(raised, boss, replier, doer):  # noqa: F811
    reply(client_for(replier), raised)
    add_action(client_for(boss), raised, doer)
    action = Action.objects.get()
    client_for(doer).post(reverse("audits:action_step", args=[action.pk, "done"]))

    body = home(client_for(boss)).content.decode()
    for label in ("Replies to write", "My actions", "To verify", "To close"):
        assert label in body
    assert 'id="pending-table"' in body
    assert "Raise transfer slip" in body  # the To verify table opened, as it has the work
    assert "deepa" in body or doer.full_name in body  # owner column


def test_pending_tab_switch_and_remembered(raised, replier):  # noqa: F811
    client = client_for(replier)
    response = htmx(client, "pending-items", tab="close")
    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="pending-items"' in body and "<html" not in body
    assert "No queries ready to close." in body
    # Next visit with no tab in the URL opens the same tab.
    assert client.session["pending_tab"] == "close"
    assert "No queries ready to close." in home(client).content.decode()


def test_pending_table_filters_sort_and_partial(raised, replier):  # noqa: F811
    client = client_for(replier)
    response = htmx(client, "pending-table", tab="replies", item_code="A100", sort="-created_at")
    body = response.content.decode()
    assert 'id="pending-table"' in body
    assert 'id="pending-items"' not in body and 'role="tablist"' not in body  # only the table
    assert "A100" in body and "<html" not in body
    assert (
        "A100" not in htmx(client, "pending-table", tab="replies", item_code="ZZZ").content.decode()
    )


def test_pending_csv(raised, replier):  # noqa: F811
    response = home(client_for(replier), tab="replies", export="csv")
    assert response["Content-Type"].startswith("text/csv")
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Branch,Audit,Audit date,Item No")
    assert "A100" in text


def test_pending_no_longer_capped_at_50(raised, replier):  # noqa: F811
    from audits.queries import pending_for

    pending = pending_for(replier, raised.audit.branch.company)
    assert pending["to_reply"].query.high_mark is None  # no slice: the table pages instead


def test_pending_bad_tab_falls_back(raised, replier):  # noqa: F811
    body = home(client_for(replier), tab="nope").content.decode()
    assert "A100" in body  # Replies tab (the one with work) is shown


def test_pending_nothing_waiting(doer):  # noqa: F811
    assert "Nothing waiting for you" in home(client_for(doer)).content.decode()


# ---------- Dashboard drilldown ----------


TODAY = timezone.localdate()


def detail(client, kind, **params):
    return client.get(reverse("audits:dashboard_detail", args=[kind]), params)


@pytest.fixture
def two_audits(admin_user, north):  # noqa: F811
    """N1 runs: X1 short in both audits, X2 excess once, X3 within tolerance once."""
    first = make_audit(
        north,
        "First",
        TODAY - timedelta(days=40),
        admin_user,
        lines=[("X1", 10, 5, 2), ("X2", 10, 14, 3), ("X3", 10, 11, 1)],
    )
    second = make_audit(north, "Second", TODAY, admin_user, lines=[("X1", 10, 7, 2)])
    return first, second


def test_variance_rows_link_to_detail(admin_client, two_audits, north):  # noqa: F811
    first, _second = two_audits
    month = first.audit_date.replace(day=1)
    response = detail(admin_client, "variance", row_branch=north.pk, month=f"{month:%Y-%m}")
    body = response.content.decode()
    assert response.status_code == 200
    # Biggest value first: X2 (+12) before X1 (−10); X3 (+1, in tolerance) is still listed.
    assert body.index("X2") < body.index("X1") < body.index("X3")
    assert reverse("audits:query_detail", args=[first.queries.get(line__item_code="X1").pk]) in body


def test_variance_tab_has_expander_urls(admin_client, two_audits):
    body = admin_client.get(reverse("audits:dashboard_tab", args=["variance"])).content.decode()
    assert reverse("audits:dashboard_detail", args=["variance"]) in body
    assert 'hx-trigger="click once"' in body


def test_repeat_detail_shows_each_audit(admin_client, two_audits, north):  # noqa: F811
    rows = dashboards.repeat_items(filters_for([north], today=TODAY))
    assert rows[0]["item_code"] == "X1" and rows[0]["streak"] == 2
    body = detail(
        admin_client, "repeat", row_branch=north.pk, item=rows[0]["item_id"]
    ).content.decode()
    assert "First" in body and "Second" in body and "Shortage" in body


def test_trend_detail_lists_audit_items(admin_client, two_audits):
    _first, second = two_audits
    body = detail(admin_client, "trend", audit=second.pk).content.decode()
    assert "X1" in body and "X2" not in body


def test_drilldown_refuses_other_branch(viewer, two_audits, south, north):  # noqa: F811
    first, _second = two_audits
    client = client_for(viewer)  # View reports in North only
    response = detail(client, "variance", row_branch=south.pk, month=f"{TODAY:%Y-%m}")
    assert (
        response.status_code == 400 and "not in your dashboard filters" in response.content.decode()
    )
    other = make_audit(south, "Hidden", TODAY, first.created_by, lines=[("X9", 10, 2, 1)])
    response = detail(client, "trend", audit=other.pk)
    assert response.status_code == 400 and "X9" not in response.content.decode()


def test_drilldown_bad_values(admin_client, north):  # noqa: F811
    assert detail(admin_client, "variance", row_branch=north.pk, month="soon").status_code == 400
    assert detail(admin_client, "repeat", row_branch=north.pk).status_code == 400
    assert detail(admin_client, "nope").status_code == 404


def test_drilldown_needs_view_reports(raised, replier):  # noqa: F811
    response = detail(client_for(replier), "trend", audit=raised.audit.pk)
    body = response.content.decode()
    # A short message for the expander (shown by dashboard.js), not a whole page.
    assert response.status_code == 403 and "View reports" in body and "<html" not in body


def test_drilldown_boxes_marked_for_error_messages(admin_client, two_audits):
    body = admin_client.get(reverse("audits:dashboard_tab", args=["variance"])).content.decode()
    assert "data-drilldown" in body


def test_trend_item_mode_has_no_expander(admin_client, two_audits):
    url = reverse("audits:dashboard_tab", args=["trend"])
    assert "dashboard/detail/trend" in admin_client.get(url).content.decode()
    assert "dashboard/detail/trend" not in admin_client.get(url, {"item": "X1"}).content.decode()


def test_ageing_link_carries_audit_type(admin_client, admin_user, north):  # noqa: F811
    make_audit(north, "Plan", TODAY, admin_user, lines=[("P1", 10, 2, 1)])
    make_audit(north, "Spot", TODAY, admin_user, lines=[("S1", 10, 2, 1)], kind="surprise")
    filters = filters_for([north], today=TODAY, type="surprise")
    link = dashboard_views._ageing_link(filters, north.code, today=TODAY)
    assert "audit_type=surprise" in link
    listing = admin_client.get(link).content.decode()
    assert "S1" in listing and "P1" not in listing


def test_ageing_links_open_matching_queries(admin_client, admin_user, north):  # noqa: F811
    aged = make_audit(
        north, "Aged", TODAY - timedelta(days=5), admin_user, lines=[("X1", 10, 5, 2)]
    )
    query = aged.queries.get()
    query.created_at = timezone.now() - timedelta(days=45)  # in the 31–60 group
    query.save()

    filters = filters_for([north], today=TODAY)
    link = dashboard_views._ageing_link(filters, north.code, 31, 60, today=TODAY)
    params = parse_qs(urlparse(link).query)
    assert params["created_at_min"] == [(TODAY - timedelta(days=60)).isoformat()]
    assert params["created_at_max"] == [(TODAY - timedelta(days=31)).isoformat()]
    assert "closed" not in params["status"]

    listing = admin_client.get(link).content.decode()
    assert "X1" in listing
    too_new = dashboard_views._ageing_link(filters, north.code, 0, 7, today=TODAY)
    assert "X1" not in admin_client.get(too_new).content.decode()

    page = admin_client.get(reverse("audits:dashboard_tab", args=["queries"])).content.decode()
    assert "created_at_min" in page  # the counts are links
