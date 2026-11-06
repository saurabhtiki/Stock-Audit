"""Screens for Phase 4: query list, query page, reply, updates, actions, close, reopen."""

import logging

from django.contrib import messages
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.access import visible_branches
from audits import queries as query_logic
from audits.forms import ActionForm
from audits.models import Action, Query
from audits.permissions import (
    can_add_action,
    can_edit_reply,
    can_mark_done,
    can_reply,
    can_verify_close,
    forbidden,
    get_visible_audit,
)
from audits.views import _is_htmx, _vary_on_htmx
from core.datatable import CHOICE, DATE, NUMBER, Column, DataTable

logger = logging.getLogger(__name__)

NO_REPLY_RIGHT = (
    "You need the “Reply” right in this Branch, and the audit must be at Checking Done."
)
NO_ACTION_RIGHT = (
    "You need the “Actions” right in this Branch (or be the action's owner), "
    "and the audit must be at Checking Done."
)
NO_VERIFY_RIGHT = (
    "You need the “Verify & close” right in this Branch, and the audit must be at Checking Done."
)

QUERY_COLUMNS = [
    Column("branch_code", "Branch", type=CHOICE),
    Column("audit_title", "Audit"),
    Column("audit_date", "Audit date", type=DATE),
    Column("item_code", "Item No"),
    Column("description", "Description"),
    Column("variance_qty", "Variance", type=NUMBER),
    Column("variance_value", "Var. value", type=NUMBER),
    Column("status", "Status", type=CHOICE, choices=dict(Query.Status.choices)),
    Column("open_actions", "Open actions", type=NUMBER),
    Column("overdue_actions", "Overdue", type=NUMBER),
]
QUERY_SHORTCUTS = [
    ("All", "status", "", "Show every query"),
    ("Open", "status", "open", "Waiting for a reply"),
    ("Reply Submitted", "status", "reply_submitted", "Replied; waiting for review or actions"),
    ("Action Pending", "status", "action_pending", "Has actions not yet closed off"),
    ("Closed", "status", "closed", "Settled; carried forward no more"),
]


def _query_rows(branches):
    not_verified = ~Q(actions__status=Action.Status.VERIFIED)
    return Query.objects.filter(audit__branch__in=branches).annotate(
        branch_code=F("audit__branch__code"),
        audit_title=F("audit__title"),
        audit_date=F("audit__audit_date"),
        item_code=F("line__item_code"),
        description=F("line__description"),
        variance_qty=F("line__variance_qty"),
        variance_value=F("line__variance_value"),
        open_actions=Count("actions", filter=not_verified),
        overdue_actions=Count(
            "actions",
            filter=not_verified & Q(actions__due_date__lt=timezone.localdate()),
        ),
    )


def query_list(request):
    company = request.company
    branches = visible_branches(request.user, company)
    if company is None or not branches.exists():
        return forbidden(request, "You need access to a Branch of this company to see its queries.")
    table = DataTable(
        request.GET,
        _query_rows(branches),
        QUERY_COLUMNS,
        table_id="query-table",
        default_sort=("audit__audit_date", "line__item_code"),
        search_fields=("line__item_code", "line__description", "audit__title"),
        search_placeholder="Search item code, description or audit",
        shortcuts=QUERY_SHORTCUTS,
        base_url=request.path,
        noun="queries",
    )
    if table.wants_csv:
        return table.csv_response(f"queries_{timezone.localdate().isoformat()}.csv")
    template = "audits/partials/query_table.html" if _is_htmx(request) else "audits/query_list.html"
    return _vary_on_htmx(render(request, template, {"table": table}))


# ---------- Query page ----------


def _visible_query(request, pk) -> Query:
    query = get_object_or_404(Query.objects.select_related("line", "item"), pk=pk)
    query.audit = get_visible_audit(request, query.audit_id)
    return query


def query_detail(request, pk):
    query = _visible_query(request, pk)
    user = request.user
    actions = list(query.actions.select_related("owner", "done_by", "verified_by", "created_by"))
    for action in actions:
        action.query = query
        action.can_mark_done = can_mark_done(user, action)
    can_close_right = can_verify_close(user, query)
    context = {
        "query": query,
        "audit": query.audit,
        "line": query.line,
        "reply": getattr(query, "reply", None),
        "updates": query.updates.select_related("created_by"),
        "actions": actions,
        "history": query_logic.item_history(query),
        "can_reply": can_reply(user, query),
        "can_edit_reply": can_edit_reply(user, query),
        "can_add_action": can_add_action(user, query),
        "can_verify": can_close_right,
        "can_close": can_close_right and not query.is_closed,
        "close_blocker": query_logic.close_blocker(query),
        "can_reopen": can_close_right and query.is_closed,
        "owners": query_logic.owner_choices(query) if can_add_action(user, query) else [],
        "today": timezone.localdate(),
    }
    return render(request, "audits/query_detail.html", context)


def _run(request, query_pk, change, success: str):
    """Run one change from audits.queries; show its message and go back to the query page."""
    try:
        change()
    except query_logic.QueryError as error:
        logger.info("Query %s change refused for %s: %s", query_pk, request.user.username, error)
        messages.error(request, str(error))
    else:
        messages.success(request, success)
    return redirect("audits:query_detail", pk=query_pk)


@require_POST
def query_reply(request, pk):
    query = _visible_query(request, pk)
    if not can_reply(request.user, query):
        return forbidden(request, NO_REPLY_RIGHT)
    text = request.POST.get("text", "")
    if getattr(query, "reply", None) is None:
        change = lambda: query_logic.submit_reply(request.user, query.pk, text)
        return _run(request, query.pk, change, "Reply submitted.")
    change = lambda: query_logic.add_update(request.user, query.pk, text)
    return _run(request, query.pk, change, "Update added.")


@require_POST
def query_reply_edit(request, pk):
    query = _visible_query(request, pk)
    if not can_edit_reply(request.user, query):
        return forbidden(
            request, "Only the person who wrote the reply can change it, until Closed."
        )
    text = request.POST.get("text", "")
    return _run(
        request,
        query.pk,
        lambda: query_logic.edit_reply(request.user, query.pk, text),
        "Reply saved.",
    )


@require_POST
def action_add(request, pk):
    query = _visible_query(request, pk)
    if not can_add_action(request.user, query):
        return forbidden(request, NO_ACTION_RIGHT)
    form = ActionForm(request.POST)
    if not form.is_valid():
        logger.info("Action not added on query %s: %s", query.pk, form.errors.as_text())
        messages.error(request, form.errors["due_date"][0])
        return redirect("audits:query_detail", pk=query.pk)
    owner_id = request.POST.get("owner", "")
    owner_id = int(owner_id) if owner_id.isdecimal() and owner_id.isascii() else None
    change = lambda: query_logic.add_action(
        request.user,
        query.pk,
        request.POST.get("description", ""),
        owner_id,
        form.cleaned_data["due_date"],
    )
    return _run(request, query.pk, change, "Action added.")


ACTION_STEPS = {
    "done": (query_logic.mark_done, "Action marked Done."),
    "undo": (query_logic.undo_done, "Action set back to Open."),
    "verify": (query_logic.verify_action, "Action Verified."),
}


@require_POST
def action_step(request, pk, step):
    action = get_object_or_404(Action.objects.select_related("query"), pk=pk)
    query = _visible_query(request, action.query_id)
    action.query = query
    if step not in ACTION_STEPS:
        return forbidden(request, "Unknown action step.")
    if step == "verify":
        if not can_verify_close(request.user, query):
            return forbidden(request, NO_VERIFY_RIGHT)
    elif not can_mark_done(request.user, action):
        return forbidden(request, NO_ACTION_RIGHT)
    run, success = ACTION_STEPS[step]
    return _run(request, query.pk, lambda: run(request.user, action.pk), success)


@require_POST
def query_close(request, pk):
    query = _visible_query(request, pk)
    if not can_verify_close(request.user, query):
        return forbidden(request, NO_VERIFY_RIGHT)
    note = request.POST.get("note", "")
    return _run(
        request,
        query.pk,
        lambda: query_logic.close_query(request.user, query.pk, note),
        "Query Closed.",
    )


@require_POST
def query_reopen(request, pk):
    query = _visible_query(request, pk)
    if not can_verify_close(request.user, query):
        return forbidden(request, NO_VERIFY_RIGHT)
    reason = request.POST.get("reason", "")
    return _run(
        request,
        query.pk,
        lambda: query_logic.reopen_query(request.user, query.pk, reason),
        "Query reopened. It is Open again.",
    )
