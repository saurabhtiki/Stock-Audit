"""Dashboard page: one filter bar and four tabs (Phase 6, 9). Numbers come from audits.dashboards."""

import logging
import re
from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django_datatable import (
    CHOICE,
    DATE,
    NUMBER,
    Column,
    DataTable,
    RowsTable,
    rows_csv_response,
)

from audits import dashboards
from audits.models import Action, Audit, AuditLine, Item, Query
from audits.permissions import branches_with_right, forbidden
from audits.views import _is_htmx, _vary_on_htmx

logger = logging.getLogger(__name__)

OPEN_STATUSES = [status for status in Query.Status.values if status != Query.Status.CLOSED]

TABS = (
    ("status", "Audit status", "Every audit with its stage, % counted and open queries"),
    (
        "queries",
        "Queries & actions",
        "How old the open queries are, and actions past their due date",
    ),
    (
        "trend",
        "Trend",
        "Excess, shortage and net variance over time: per audit for one Branch, per month for more",
    ),
    ("repeat", "Repeat items", "Items with Excess / Shortage in 2 or more audits in a row"),
)
# Tabs that use the Item filter (Status lists audits, so items mean nothing there).
ITEM_FILTER_TABS = {"queries", "trend", "repeat"}
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


OVERDUE_COLUMNS = [
    Column("branch_code", "Branch", type=CHOICE),
    Column("audit_title", "Audit"),
    Column("item_code", "Item No"),
    Column("description", "Action"),
    Column("owner_name", "Owner", type=CHOICE),
    Column("status", "Status", type=CHOICE, choices=dict(Action.Status.choices)),
    Column("due_date", "Due", type=DATE),
    Column(
        "days_late",
        "Days late",
        type=NUMBER,
        filterable=False,  # a day count worked out from today; filter on "Due" instead
        csv_value=lambda action: action.days_late.days,
    ),
]

CELL_LABELS = ("6th last", "5th last", "4th last", "3rd last", "2nd last", "Latest")
CELL_CSV = {"ok": "OK", "not_counted": "Not counted", "missing": "Not in audit", "none": ""}
MIN_STREAKS = (2, 3, 4)  # the Min streak quick buttons (4 = 4 or more)


def _cell_csv(cell: dict):
    """A grid cell in the CSV: the variance qty, or a word for OK / not counted / no line."""
    if cell["state"] in ("short", "excess"):
        return cell["qty"]
    return CELL_CSV[cell["state"]]


def _repeat_columns(dates: list | None) -> list[Column]:
    """Branch, item, the 6 audit cells (real dates as headings when one Branch is picked),
    streak and value."""
    cells = [
        Column(
            f"cell{index}",
            f"{dates[index]:%d %b %y}" if dates and dates[index] else label,
            type=NUMBER,  # right-aligned heading, like the numbers under it
            sortable=False,
            filterable=False,
            csv_value=lambda row, index=index: _cell_csv(row["cells"][index]),
        )
        for index, label in enumerate(CELL_LABELS)
    ]
    return [
        Column("branch_code", "Branch", type=CHOICE),
        Column("item_code", "Item No"),
        Column("description", "Description"),
        *cells,
        Column(
            "streak",
            "Streak",
            type=NUMBER,
            csv_value=lambda row: f"{row['streak']} {row['direction']}",
        ),
        Column("total_value", "Streak value", type=NUMBER),
    ]


# Tables inside a dashboard tab: a click inside one redraws only that table.
TABLE_TEMPLATES = {
    "status-table": "audits/partials/dashboard_status_table.html",
    "overdue-table": "audits/partials/dashboard_overdue_table.html",
    "repeat-table": "audits/partials/dashboard_repeat_table.html",
}


def _csv_name(tab: str) -> str:
    return f"dashboard_{tab}_{timezone.localdate().isoformat()}.csv"


def _status_context(request, filters):
    show_cancelled = request.GET.get("cancelled") == "1"
    table = DataTable(
        request.GET,
        dashboards.status_rows(filters, include_cancelled=show_cancelled),
        STATUS_COLUMNS,
        table_id="status-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
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
    if filters.audit_type:
        params.append(("audit_type", filters.audit_type))
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
    overdue = DataTable(
        request.GET,
        dashboards.overdue_actions(filters),
        OVERDUE_COLUMNS,
        table_id="overdue-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
        default_sort=("due_date",),
        search_fields=("description", "query__line__item_code", "query__line__description"),
        search_placeholder="Search action, item code or description",
        base_url=request.path,
        noun="actions",
        keep=dashboards.FILTER_PARAMS,
        csv_params=[("part", "overdue")],
        shortcuts=[
            ("All", "status", "", "Every overdue action, Open and Done"),
            ("Open", "status", Action.Status.OPEN, "Not done yet"),
            (
                "Waiting to verify",
                "status",
                Action.Status.DONE,
                "Marked Done by the owner, but nobody has verified it yet",
            ),
        ],
    )
    return {"age_labels": age_labels, "ageing": ageing, "ageing_chart": chart, "table": overdue}


def _add_detail_links(rows, filters, kind: str, make_params) -> None:
    """Give each row the URL and element id its ▸ expander loads into."""
    base = reverse("audits:dashboard_detail", args=[kind])
    for number, row in enumerate(rows, start=1):
        row["detail_id"] = f"{kind}-detail-{number}"
        params = [*filters.as_params(), *make_params(row), ("box", row["detail_id"])]
        row["detail_url"] = f"{base}?{urlencode(params)}"


def _variance_chart(rows) -> dict:
    """Excess / Shortage bars and a Net line per month, all picked Branches added up."""
    by_month = dashboards.variance_by_month(rows)
    return {
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


def _audit_chart(rows) -> dict:
    """Excess / Shortage / Net lines per audit, with the queries raised as bars."""
    return {
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


def _trend_context(request, filters):
    """One Branch picked: one point per audit. More Branches: totals per month, with a
    Branch × month table. Picked items narrow both to those items' lines."""
    if len(filters.branches) == 1:
        rows = dashboards.trend_rows(filters, branch=filters.branches[0])
        _add_detail_links(rows, filters, "trend", lambda row: [("audit", row["audit_id"])])
        return {"mode": "audit", "rows": rows, "chart": _audit_chart(rows)}
    rows = dashboards.variance_rows(filters)
    _add_detail_links(
        rows,
        filters,
        "variance",
        lambda row: [("row_branch", row["branch_id"]), ("month", f"{row['month']:%Y-%m}")],
    )
    return {"mode": "month", "rows": rows, "chart": _variance_chart(rows)}


def _repeat_context(request, filters):
    # Runs are found in Python, so the table works on those rows (RowsTable), not on SQL.
    # Rows come longest streak first, then biggest loss.
    rows = dashboards.repeat_items(filters)
    _add_detail_links(
        rows,
        filters,
        "repeat",
        lambda row: [("row_branch", row["branch_id"]), ("row_item", row["item_id"])],
    )
    table = RowsTable(
        request.GET,
        rows,
        _repeat_columns(dashboards.grid_dates(filters)),
        table_id="repeat-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
        striped=False,  # each row has a hidden detail row under it, which would upset the stripes
        search_fields=("item_code", "description"),
        search_placeholder="Search item code or description",
        base_url=request.path,
        noun="items",
        keep=dashboards.FILTER_PARAMS,
    )
    picked_min = request.GET.get("streak_min", "")
    min_streaks = [
        {
            "label": f"{count}+" if count == MIN_STREAKS[-1] else str(count),
            "tip": f"Only items with {count} or more audits in a row",
            "url": table.url(
                drop={"streak_min", "streak_max", "page"},
                add=[("streak_min", str(count))] if count > MIN_STREAKS[0] else [],
            ),
            "active": picked_min == (str(count) if count > MIN_STREAKS[0] else "")
            and not request.GET.get("streak_max"),
        }
        for count in MIN_STREAKS
    ]
    return {"table": table, "min_streaks": min_streaks}


CONTEXT_BUILDERS = {
    "status": _status_context,
    "queries": _queries_context,
    "trend": _trend_context,
    "repeat": _repeat_context,
}


def _csv(tab: str, context: dict):
    """CSV of what the tab shows (the status grid streams its own, with all its filters)."""
    if tab == "queries":
        if context.get("csv_part") == "overdue":
            return context["table"].csv_response(_csv_name("overdue_actions"))
        return rows_csv_response(
            _csv_name("query_ageing"),
            ["Branch", *(f"{label} days" for label in context["age_labels"]), "Total open"],
            [[r["branch"], *r["buckets"], r["total"]] for r in context["ageing"]],
        )
    if tab == "repeat":
        return context["table"].csv_response(_csv_name(tab))
    if context["mode"] == "month":
        return rows_csv_response(
            _csv_name(tab),
            ["Month", "Branch", "Excess value", "Shortage value", "Net variance"],
            [
                [f"{r['month']:%Y-%m}", r["branch"], r["excess"], r["shortage"], r["net"]]
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


NO_REPORTS_RIGHT = (
    "You need the “View reports” right in a Branch of this company to see dashboards."
)


def _not_found(request, message: str):
    logger.info("%s asked for %s: %s", request.user.username, request.path, message)
    raise Http404(message)


def _report_branches(request):
    """Branches where the user has “View reports” (empty list = no dashboard at all)."""
    return list(branches_with_right(request.user, request.company, "view_reports"))


def dashboard(request, tab: str = "status"):
    if tab == "variance":  # merged into Trend (Phase 9); old bookmarks still work
        query = request.GET.urlencode()
        return redirect(
            reverse("audits:dashboard_tab", args=["trend"]) + (f"?{query}" if query else "")
        )
    if tab not in TAB_NAMES:
        _not_found(request, "No such dashboard tab.")
    allowed = _report_branches(request)
    if not allowed:
        return forbidden(request, NO_REPORTS_RIGHT)
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
        item_filter_used=tab in ITEM_FILTER_TABS,
        picked_items=[{"id": str(item.pk), "label": _item_label(item)} for item in filters.items],
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
    hx_target = request.headers.get("HX-Target", "")
    if _is_htmx(request) and hx_target in TABLE_TEMPLATES:
        template = TABLE_TEMPLATES[hx_target]
    elif _is_htmx(request):
        template = "audits/partials/dashboard_body.html"
    else:
        template = "audits/dashboard.html"
    response = _vary_on_htmx(render(request, template, context))
    patch_vary_headers(response, ["HX-Target"])
    return response


def _item_label(item) -> str:
    return f"{item.code} – {item.description}" if item.description else item.code


ITEM_SEARCH_LIMIT = 20


def dashboard_item_search(request):
    """Items whose code or description contains the typed text, for the Item filter (htmx)."""
    if not _report_branches(request):
        return forbidden(request, NO_REPORTS_RIGHT)
    text = request.GET.get("q", "").strip()
    items = Item.objects.filter(company=request.company)
    if text:
        items = items.filter(Q(code__icontains=text) | Q(description__icontains=text))
    items = list(items.order_by("code")[: ITEM_SEARCH_LIMIT + 1])
    return render(
        request,
        "audits/partials/dashboard_item_options.html",
        {
            "options": [
                {"id": str(item.pk), "label": _item_label(item)}
                for item in items[:ITEM_SEARCH_LIMIT]
            ],
            "more": len(items) > ITEM_SEARCH_LIMIT,
            "text": text,
        },
    )


DETAIL_KINDS = ("variance", "trend", "repeat")


def _detail_problem(request, message: str, status: int = 400):
    """A short message shown inside the expander (dashboard.js lets htmx swap 4xx in there)."""
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
        _not_found(request, "No such drilldown.")
    allowed = _report_branches(request)
    if not allowed:
        return _detail_problem(request, NO_REPORTS_RIGHT, status=403)
    filters = dashboards.read_filters(request.GET, allowed)
    context = {"kind": kind}

    if kind == "repeat":
        return _repeat_detail(request, filters)
    if kind == "variance":
        branch_id = _int_param(request.GET, "row_branch")
        if branch_id not in filters.branch_ids:
            return _detail_problem(
                request, "This Branch is not in your dashboard filters, so its rows are hidden."
            )
        month = dashboards._month_start(request.GET.get("month", ""))
        if month is None:
            return _detail_problem(request, "The month is missing or not like 2026-04.")
        lines = dashboards.variance_detail(filters, branch_id, month)
    else:
        audit_id = _int_param(request.GET, "audit")
        if audit_id is None or not filters.final_audits().filter(pk=audit_id).exists():
            return _detail_problem(
                request, "This audit is not a counted audit inside your dashboard filters."
            )
        lines = dashboards.audit_detail_lines(filters, audit_id)

    table = _detail_table(request, kind, lines)
    if table.wants_csv:
        logger.info("%s downloaded dashboard drilldown CSV %s", request.user.username, kind)
        return table.csv_response(_csv_name(f"{kind}_items"))
    context["table"] = table
    template = (
        "audits/partials/dashboard_detail_table.html"
        if request.headers.get("HX-Target") == table.table_id
        else "audits/partials/dashboard_detail.html"
    )
    response = render(request, template, context)
    patch_vary_headers(response, ["HX-Target"])
    return response


def _repeat_detail(request, filters):
    """Every audit of one item in one Branch (the repeat row's panel), newest first."""
    branch_id = _int_param(request.GET, "row_branch")
    if branch_id not in filters.branch_ids:
        return _detail_problem(
            request, "This Branch is not in your dashboard filters, so its rows are hidden."
        )
    item_id = _int_param(request.GET, "row_item")
    if item_id is None or not Item.objects.filter(pk=item_id, company=request.company).exists():
        return _detail_problem(request, "This item is not in this company.")
    lines = dashboards.repeat_history(filters, branch_id, item_id)
    return render(request, "audits/partials/dashboard_repeat_detail.html", {"lines": lines})


BOX_ID = re.compile(r"[a-z]+-detail-\d+")


def _detail_table(request, kind: str, lines) -> DataTable:
    """The expander's rows as our usual table (filters, sort, pages, CSV). Its clicks reload
    only this table and do not change the page address, since it lives inside the dashboard."""
    box = request.GET.get("box", "")
    columns = [
        Column("item_code", "Item No"),
        Column("description", "Description"),
        Column("count_status", "Status", type=CHOICE, choices=dict(AuditLine.Status.choices)),
        Column("variance_qty", "Variance qty", type=NUMBER),
        Column("variance_value", "Variance value", type=NUMBER),
        Column(
            "query__status",
            "Query",
            type=CHOICE,
            choices=dict(Query.Status.choices),
            # No query (inside the tolerance): getattr gives None instead of an error.
            csv_value=lambda line: getattr(line, "query", None) and line.query.get_status_display(),
        ),
    ]
    if kind == "variance":
        columns.insert(0, Column("audit__title", "Audit", csv_value=lambda line: line.audit.title))
    return DataTable(
        request.GET,
        lines,
        columns,
        table_id=f"{box if BOX_ID.fullmatch(box) else kind + '-detail'}-table",
        density_switch=True,
        column_toggle=True,
        border_toggle=True,
        default_sort=("-size", "item_code"),
        base_url=request.path,
        noun="items",
        keep=(*dashboards.FILTER_PARAMS, "row_branch", "month", "audit", "box"),
        push_url=False,
    )
