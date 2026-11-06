"""Dashboard page: one filter bar and five tabs (Phase 6). Numbers come from audits.dashboards."""

import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from audits import dashboards
from audits.models import Audit, Query
from audits.permissions import branches_with_right, forbidden
from audits.views import _is_htmx, _vary_on_htmx
from core.datatable import CHOICE, DATE, NUMBER, Column, DataTable, rows_csv_response

logger = logging.getLogger(__name__)

OPEN_STATUSES = [status for status in Query.Status.values if status != Query.Status.CLOSED]

TABS = (
    ("status", "Audit status", "Every audit with its stage, % counted and open queries"),
    (
        "queries",
        "Queries & actions",
        "How old the open queries are, and actions past their due date",
    ),
    ("variance", "Variance", "Excess, shortage and net variance value by Branch and month"),
    ("repeat", "Repeat items", "Items with Excess / Shortage in 2 or more audits in a row"),
    ("trend", "Trend", "Variance per audit over time, for one Branch or one item"),
)
TAB_NAMES = {name for name, _label, _tip in TABS}

STATUS_COLUMNS = [
    Column("branch_code", "Branch", type=CHOICE),
    Column("title", "Audit"),
    Column("audit_date", "Audit date", type=DATE),
    Column("audit_type", "Type", type=CHOICE, choices=dict(Audit.Type.choices)),
    Column("stage", "Stage", type=CHOICE, choices=dict(Audit.Stage.choices)),
    Column("line_count", "Lines", type=NUMBER),
    Column("pct_counted", "% counted", type=NUMBER),
    Column("open_queries", "Open queries", type=NUMBER),
]


def _csv_name(tab: str) -> str:
    return f"dashboard_{tab}_{timezone.localdate().isoformat()}.csv"


def _status_context(request, filters):
    show_cancelled = request.GET.get("cancelled") == "1"
    table = DataTable(
        request.GET,
        dashboards.status_rows(filters, include_cancelled=show_cancelled),
        STATUS_COLUMNS,
        table_id="status-table",
        default_sort=("-audit_date", "branch__code"),
        search_fields=("title", "branch__code"),
        search_placeholder="Search audit title or Branch code",
        base_url=request.path,
        noun="audits",
        keep=(*dashboards.FILTER_PARAMS, "cancelled"),
    )
    return {"table": table, "show_cancelled": show_cancelled}


def _money(value) -> float:
    return round(float(value), 2)


def _ageing_link(filters, branch_code: str, low=None, high=None, today=None) -> str:
    """The Queries list showing the open queries behind one ageing number."""
    today = today or timezone.localdate()
    params = [
        ("branch_code", branch_code),
        *[("status", status) for status in OPEN_STATUSES],
        ("audit_date_min", filters.month_from.isoformat()),
        ("audit_date_max", filters.date_to.isoformat()),
    ]
    if high is not None:
        params.append(("created_at_min", (today - timedelta(days=high)).isoformat()))
    if low is not None:
        params.append(("created_at_max", (today - timedelta(days=low)).isoformat()))
    return f"{reverse('audits:query_list')}?{urlencode(params)}"


def _queries_context(request, filters):
    age_labels = [label for label, _low, _high in dashboards.AGE_BUCKETS]
    ageing = dashboards.ageing_rows(filters)
    for row in ageing:
        row["cells"] = [
            {"count": count, "url": _ageing_link(filters, row["branch"], low, high)}
            for count, (_label, low, high) in zip(row["buckets"], dashboards.AGE_BUCKETS)
        ]
        row["total_url"] = _ageing_link(filters, row["branch"])
    chart = {
        "type": "bar",
        "stacked": True,
        "labels": [row["branch"] for row in ageing],
        "datasets": [
            {
                "label": f"{label} days",
                "data": [row["buckets"][i] for row in ageing],
                "color": f"age{i}",
            }
            for i, label in enumerate(age_labels)
        ],
    }
    return {
        "age_labels": age_labels,
        "ageing": ageing,
        "ageing_chart": chart,
        "overdue": dashboards.overdue_actions(filters),
    }


def _add_detail_links(rows, filters, kind: str, make_params) -> None:
    """Give each row the URL and element id its ▸ expander loads into."""
    base = reverse("audits:dashboard_detail", args=[kind])
    for number, row in enumerate(rows, start=1):
        row["detail_url"] = f"{base}?{urlencode([*filters.as_params(), *make_params(row)])}"
        row["detail_id"] = f"{kind}-detail-{number}"


def _variance_context(request, filters):
    rows = dashboards.variance_rows(filters)
    _add_detail_links(
        rows,
        filters,
        "variance",
        lambda row: [("row_branch", row["branch_id"]), ("month", f"{row['month']:%Y-%m}")],
    )
    by_month = dashboards.variance_by_month(rows)
    chart = {
        "type": "bar",
        "labels": [f"{total['month']:%b %y}" for total in by_month],
        "datasets": [
            {"label": "Excess", "data": [_money(t["excess"]) for t in by_month], "color": "excess"},
            {
                "label": "Shortage",
                "data": [_money(t["shortage"]) for t in by_month],
                "color": "shortage",
            },
            {
                "label": "Net",
                "data": [_money(t["net"]) for t in by_month],
                "color": "net",
                "line": True,
            },
        ],
    }
    return {"rows": rows, "by_month": by_month, "chart": chart}


def _repeat_context(request, filters):
    rows = dashboards.repeat_items(filters)
    _add_detail_links(
        rows,
        filters,
        "repeat",
        lambda row: [("row_branch", row["branch_id"]), ("item", row["item_id"])],
    )
    return {"rows": rows}


def _trend_context(request, filters):
    """Pick a Branch (default: the first picked) or type an item code."""
    item_code = request.GET.get("item", "").strip()
    item = dashboards.find_item(request.company, item_code)
    branch = None
    if item is None:
        wanted = request.GET.get("trend_branch", "")
        branch = next((b for b in filters.branches if str(b.pk) == wanted), None)
        if branch is None:
            # Start on the Branch of the latest counted audit, so the chart isn't empty.
            latest = filters.final_audits().order_by("-audit_date", "-id").first()
            branch = latest.branch if latest else filters.branches[0]
    rows = dashboards.trend_rows(filters, branch=branch, item=item)
    _add_detail_links(rows, filters, "trend", lambda row: [("audit", row["audit_id"])])
    chart = {
        "type": "line",
        "labels": [f"{row['label']} ({row['audit_date']:%d %b %y})" for row in rows],
        "datasets": [
            {"label": "Excess", "data": [_money(r["excess"]) for r in rows], "color": "excess"},
            {
                "label": "Shortage",
                "data": [_money(r["shortage"]) for r in rows],
                "color": "shortage",
            },
            {"label": "Net", "data": [_money(r["net"]) for r in rows], "color": "net"},
            {
                "label": "Queries raised",
                "data": [r["queries"] for r in rows],
                "color": "queries",
                "bar": True,
                "right_axis": True,
            },
        ],
    }
    return {
        "item_code": item_code,
        "item": item,
        "item_missing": bool(item_code) and item is None,
        "trend_branch": branch,
        "rows": rows,
        "chart": chart,
    }


CONTEXT_BUILDERS = {
    "status": _status_context,
    "queries": _queries_context,
    "variance": _variance_context,
    "repeat": _repeat_context,
    "trend": _trend_context,
}


def _csv(tab: str, context: dict):
    """CSV of what the tab shows (the status grid streams its own, with all its filters)."""
    if tab == "queries":
        if context.get("csv_part") == "overdue":
            return rows_csv_response(
                _csv_name("overdue_actions"),
                [
                    "Branch",
                    "Audit",
                    "Item No",
                    "Action",
                    "Owner",
                    "Status",
                    "Due date",
                    "Days late",
                ],
                [
                    [
                        r["branch"],
                        r["audit"],
                        r["item_code"],
                        r["description"],
                        r["owner"],
                        r["status"],
                        r["due_date"],
                        r["days_late"],
                    ]
                    for r in context["overdue"]
                ],
            )
        return rows_csv_response(
            _csv_name("query_ageing"),
            ["Branch", *(f"{label} days" for label in context["age_labels"]), "Total open"],
            [[r["branch"], *r["buckets"], r["total"]] for r in context["ageing"]],
        )
    if tab == "variance":
        return rows_csv_response(
            _csv_name(tab),
            ["Month", "Branch", "Excess value", "Shortage value", "Net variance"],
            [
                [f"{r['month']:%Y-%m}", r["branch"], r["excess"], r["shortage"], r["net"]]
                for r in context["rows"]
            ],
        )
    if tab == "repeat":
        return rows_csv_response(
            _csv_name(tab),
            ["Branch", "Item No", "Description", "Audits in a row", "Total variance value"],
            [
                [r["branch"], r["item_code"], r["description"], r["streak"], r["total_value"]]
                for r in context["rows"]
            ],
        )
    return rows_csv_response(
        _csv_name(tab),
        [
            "Audit",
            "Audit date",
            "Type",
            "Excess value",
            "Shortage value",
            "Net variance",
            "Queries",
        ],
        [
            [
                r["label"],
                r["audit_date"],
                r["audit_type"],
                r["excess"],
                r["shortage"],
                r["net"],
                r["queries"],
            ]
            for r in context["rows"]
        ],
    )


def dashboard(request, tab: str = "status"):
    if tab not in TAB_NAMES:
        raise Http404("No such dashboard tab.")
    allowed = branches_with_right(request.user, request.company, "view_reports")
    if not allowed:
        return forbidden(
            request,
            "You need the “View reports” right in a Branch of this company to see dashboards.",
        )
    filters = dashboards.read_filters(request.GET, allowed)
    context = CONTEXT_BUILDERS[tab](request, filters)

    if request.GET.get("export") == "csv":
        logger.info("%s downloaded dashboard CSV %s", request.user.username, tab)
        if tab == "status":
            return context["table"].csv_response(_csv_name(tab))
        context["csv_part"] = request.GET.get("part", "")
        return _csv(tab, context)

    filter_query = urlencode(filters.as_params())
    context.update(
        tab=tab,
        tabs=[
            {
                "name": name,
                "label": label,
                "tip": tip,
                "url": f"{reverse('audits:dashboard_tab', args=[name])}?{filter_query}",
            }
            for name, label, tip in TABS
        ],
        filters=filters,
        filter_query=filter_query,
        audit_types=Audit.Type.choices,
        # Same page and choices (filters, trend Branch / item), as a CSV download.
        csv_url=request.path
        + "?"
        + urlencode(
            [
                (key, value)
                for key, values in request.GET.lists()
                if key not in ("export", "part")
                for value in values
            ]
            + [("export", "csv")]
        ),
    )
    if _is_htmx(request) and request.headers.get("HX-Target") == "status-table":
        template = "audits/partials/dashboard_status_table.html"
    elif _is_htmx(request):
        template = "audits/partials/dashboard_body.html"
    else:
        template = "audits/dashboard.html"
    response = _vary_on_htmx(render(request, template, context))
    patch_vary_headers(response, ["HX-Target"])
    return response


DETAIL_KINDS = ("variance", "repeat", "trend")


def _detail_problem(request, message: str, status: int = 400):
    logger.info("Dashboard drilldown refused for %s: %s", request.user.username, message)
    return render(
        request, "audits/partials/dashboard_detail.html", {"problem": message}, status=status
    )


def _int_param(params, name: str) -> int | None:
    value = params.get(name, "")
    return int(value) if value.isdecimal() else None


def dashboard_detail(request, kind: str):
    """The rows behind one summary row, opened with the ▸ button (htmx, loaded on first click)."""
    if kind not in DETAIL_KINDS:
        raise Http404("No such drilldown.")
    allowed = branches_with_right(request.user, request.company, "view_reports")
    if not allowed:
        return forbidden(
            request,
            "You need the “View reports” right in a Branch of this company to see dashboards.",
        )
    filters = dashboards.read_filters(request.GET, allowed)
    context = {"kind": kind, "limit": dashboards.DETAIL_LIMIT}

    if kind in ("variance", "repeat"):
        branch_id = _int_param(request.GET, "row_branch")
        if branch_id not in filters.branch_ids:
            return _detail_problem(
                request, "This Branch is not in your dashboard filters, so its rows are hidden."
            )
    if kind == "variance":
        month = dashboards._month_start(request.GET.get("month", ""))
        if month is None:
            return _detail_problem(request, "The month is missing or not like 2026-04.")
        lines = dashboards.variance_detail(filters, branch_id, month)
    elif kind == "trend":
        audit_id = _int_param(request.GET, "audit")
        if audit_id is None or not filters.final_audits().filter(pk=audit_id).exists():
            return _detail_problem(
                request, "This audit is not a counted audit inside your dashboard filters."
            )
        lines = dashboards.audit_detail_lines(filters, audit_id)
    else:
        item_id = _int_param(request.GET, "item")
        if item_id is None:
            return _detail_problem(request, "The item is missing.")
        context["runs"] = dashboards.item_runs(filters, branch_id, item_id)
        return render(request, "audits/partials/dashboard_detail.html", context)

    context["total"] = lines.count()
    context["lines"] = lines[: dashboards.DETAIL_LIMIT]
    return render(request, "audits/partials/dashboard_detail.html", context)
