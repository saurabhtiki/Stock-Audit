"""“My pending items” on the home page: four tabs, each a full table (search, filters, CSV).

The rows come from audits.queries.pending_for, so the rights rules stay in one place.
"""

import logging
from dataclasses import dataclass

from django.db.models import Count, F, Value
from django.db.models.functions import Coalesce, NullIf
from django.shortcuts import render
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from audits.models import Query
from audits.queries import pending_for
from audits.views import _is_htmx, _vary_on_htmx
from core.datatable import CHOICE, DATE, NUMBER, Column, DataTable

logger = logging.getLogger(__name__)

SESSION_TAB_KEY = "pending_tab"


@dataclass(frozen=True)
class PendingTab:
    name: str  # in the URL: ?tab=verify
    label: str
    tip: str
    key: str  # key in pending_for()
    badge: str  # DaisyUI badge colour
    noun: str
    empty: str


TABS = (
    PendingTab(
        "replies",
        "Replies to write",
        "Open queries in Branches where you have the Reply right",
        "to_reply",
        "badge-error",
        "queries",
        "No queries waiting for your reply.",
    ),
    PendingTab(
        "actions",
        "My actions",
        "Open actions where you are the owner",
        "my_actions",
        "badge-warning",
        "actions",
        "No open actions for you.",
    ),
    PendingTab(
        "verify",
        "To verify",
        "Done actions waiting for you to check and Verify",
        "to_verify",
        "badge-info",
        "actions",
        "No done actions waiting for your check.",
    ),
    PendingTab(
        "close",
        "To close",
        "Replied queries with no unverified actions: review and close them",
        "to_close",
        "badge-success",
        "queries",
        "No queries ready to close.",
    ),
)
TAB_BY_NAME = {tab.name: tab for tab in TABS}

QUERY_SEARCH = ("line__item_code", "line__description", "audit__title")
ACTION_SEARCH = ("query__line__item_code", "description", "query__audit__title")

COLUMNS = {
    "replies": [
        Column("branch_code", "Branch", type=CHOICE),
        Column("audit_title", "Audit"),
        Column("audit_date", "Audit date", type=DATE),
        Column("item_code", "Item No"),
        Column("description", "Description"),
        Column("variance_qty", "Variance qty", type=NUMBER),
        Column("variance_value", "Variance value", type=NUMBER),
        Column("created_at", "Raised on", type=DATE),
    ],
    "actions": [
        Column("branch_code", "Branch", type=CHOICE),
        Column("audit_title", "Audit"),
        Column("item_code", "Item No"),
        Column("description", "Action"),
        Column("due_date", "Due date", type=DATE),
        Column("created_at", "Added on", type=DATE),
    ],
    "verify": [
        Column("branch_code", "Branch", type=CHOICE),
        Column("audit_title", "Audit"),
        Column("item_code", "Item No"),
        Column("description", "Action"),
        Column("owner_name", "Owner", type=CHOICE),
        Column("done_at", "Done on", type=DATE),
        Column("due_date", "Due date", type=DATE),
    ],
    "close": [
        Column("branch_code", "Branch", type=CHOICE),
        Column("audit_title", "Audit"),
        Column("audit_date", "Audit date", type=DATE),
        Column("item_code", "Item No"),
        Column("description", "Description"),
        Column("variance_value", "Variance value", type=NUMBER),
        Column("status", "Status", type=CHOICE, choices=dict(Query.Status.choices)),
        Column("replied_at", "Replied on", type=DATE),
        Column("action_count", "Actions", type=NUMBER),
    ],
}


def _query_rows(queryset):
    return queryset.annotate(
        branch_code=F("audit__branch__code"),
        audit_title=F("audit__title"),
        audit_date=F("audit__audit_date"),
        item_code=F("line__item_code"),
        description=F("line__description"),
        variance_qty=F("line__variance_qty"),
        variance_value=F("line__variance_value"),
    )


def _action_rows(queryset):
    return queryset.annotate(
        branch_code=F("query__audit__branch__code"),
        audit_title=F("query__audit__title"),
        item_code=F("query__line__item_code"),
        owner_name=Coalesce(NullIf(F("owner__full_name"), Value("")), F("owner__username")),
    )


def _rows(tab: PendingTab, queryset):
    if tab.name == "replies":
        return _query_rows(queryset)
    if tab.name == "close":
        return _query_rows(queryset).annotate(
            replied_at=F("reply__created_at"), action_count=Count("actions", distinct=True)
        )
    return _action_rows(queryset)


def _default_sort(tab: PendingTab) -> tuple:
    if tab.name == "actions":
        return (F("due_date").asc(nulls_last=True), "created_at")
    if tab.name == "verify":
        return ("done_at",)
    return ("created_at",)


def _pick_tab(request, counts: dict) -> PendingTab:
    """The tab in the URL, else the one opened last time, else the first with work in it."""
    wanted = request.GET.get("tab") or request.session.get(SESSION_TAB_KEY, "")
    if wanted in TAB_BY_NAME:
        return TAB_BY_NAME[wanted]
    return next((tab for tab in TABS if counts[tab.name]), TABS[0])


def pending_section(request):
    """For the home page: a context dict, or a ready response (CSV, or an htmx part)."""
    pending = pending_for(request.user, request.company)
    counts = {tab.name: pending[tab.key].count() for tab in TABS}
    tab = _pick_tab(request, counts)
    request.session[SESSION_TAB_KEY] = tab.name
    table = DataTable(
        request.GET,
        _rows(tab, pending[tab.key]),
        COLUMNS[tab.name],
        table_id="pending-table",
        default_sort=_default_sort(tab),
        search_fields=QUERY_SEARCH if tab.name in ("replies", "close") else ACTION_SEARCH,
        search_placeholder="Search item code, description or audit",
        base_url=request.path,
        noun=tab.noun,
        keep=("tab",),
    )
    if table.wants_csv:
        logger.info("%s downloaded pending items CSV %s", request.user.username, tab.name)
        return table.csv_response(f"my_{tab.name}_{timezone.localdate().isoformat()}.csv")

    context = {
        "pending_tab": tab,
        "pending_table": table,
        "pending_tabs": [{"tab": item, "count": counts[item.name]} for item in TABS],
        "pending_total": sum(counts.values()),
        "today": timezone.localdate(),
    }
    target = request.headers.get("HX-Target") if _is_htmx(request) else None
    if target in ("pending-items", "pending-table"):
        template = (
            "audits/partials/pending_items.html"
            if target == "pending-items"
            else "audits/partials/pending_table.html"
        )
        response = _vary_on_htmx(render(request, template, context))
        patch_vary_headers(response, ["HX-Target"])
        return response
    return context
