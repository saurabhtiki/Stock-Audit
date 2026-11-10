"""Screens for Phase 4: query list, query page, reply, updates, actions, close, reopen."""

import logging

from django.contrib import messages
from django.db.models import Count, F, Q
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_datatable import CHOICE, DATE, NUMBER, Column, DataTable

from accounts.access import visible_branches
from audits import queries as query_logic
from audits.forms import ActionForm
from audits.models import Action, Audit, Query
from audits.permissions import (
    QUERY_STAGES,
    branches_with_right,
    can_add_action,
    can_edit_reply,
    can_mark_done,
    can_reopen_query,
    can_reply,
    can_verify_close,
    forbidden,
    get_visible_audit,
)
from audits.views import _is_htmx, _posted_ids, _vary_on_htmx

logger = logging.getLogger(__name__)

NO_REPLY_RIGHT = (
    "You need the “Reply” right in this Branch, and the audit must be at Checking Done or Locked."
)
NO_ACTION_RIGHT = (
    "You need the “Actions” right in this Branch (or be the action's owner), "
    "and the audit must be at Checking Done or Locked."
)
NO_VERIFY_RIGHT = (
    "You need the “Verify & close” right in this Branch, "
    "and the audit must be at Checking Done or Locked."
)

QUERY_COLUMNS = [
    Column("branch_code", "Branch", type=CHOICE),
    Column("audit_title", "Audit"),
    Column("audit_date", "Audit date", type=DATE),
    Column("audit_type", "Type", type=CHOICE, choices=dict(Audit.Type.choices)),
    Column("item_code", "Item No"),
    Column("description", "Description"),
    Column("variance_qty", "Variance", type=NUMBER),
    Column("variance_value", "Var. value", type=NUMBER),
    Column("status", "Status", type=CHOICE, choices=dict(Query.Status.choices)),
    Column("created_at", "Raised on", type=DATE),
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
        audit_type=F("audit__audit_type"),
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


def _query_table(branches, params, base_url="") -> DataTable:
    return DataTable(
        params,
        _query_rows(branches),
        QUERY_COLUMNS,
        table_id="query-table",
        density_switch=True,
        column_toggle=True,
        sticky_header=True,
        default_sort=("audit__audit_date", "line__item_code"),
        search_fields=("line__item_code", "line__description", "audit__title"),
        search_placeholder="Search item code, description or audit",
        shortcuts=QUERY_SHORTCUTS,
        base_url=base_url,
        noun="queries",
    )


def _bulk_rights(user, company) -> dict:
    return {
        "reply": branches_with_right(user, company, "reply_queries"),
        "close": branches_with_right(user, company, "verify_close_queries"),
    }


def _pickable(table: DataTable, rights: dict):
    """Queries of the list that can be ticked for a common reply or close: not Closed,
    audit at Checking Done or Locked, and the user has Reply or Verify in its Branch."""
    return table.filtered.exclude(status=Query.Status.CLOSED).filter(
        audit__stage__in=QUERY_STAGES,
        audit__branch__in=[*rights["reply"], *rights["close"]],
    )


def query_list(request):
    company = request.company
    branches = visible_branches(request.user, company)
    if company is None or not branches.exists():
        return forbidden(request, "You need access to a Branch of this company to see its queries.")
    table = _query_table(branches, request.GET, request.path)
    if table.wants_csv:
        return table.csv_response(f"queries_{timezone.localdate().isoformat()}.csv")
    rights = _bulk_rights(request.user, company)
    can_bulk = bool(rights["reply"] or rights["close"])
    context = {"table": table, "can_bulk": can_bulk}
    if can_bulk:
        pickable = _pickable(table, rights)
        page_ids = [row.pk for row in table.page]
        context.update(
            can_bulk_reply=bool(rights["reply"]),
            can_bulk_close=bool(rights["close"]),
            matching_count=pickable.count(),
            pickable_ids=set(pickable.filter(pk__in=page_ids).values_list("pk", flat=True)),
            bulk_max=BULK_MAX,
        )
    template = "audits/partials/query_table.html" if _is_htmx(request) else "audits/query_list.html"
    return _vary_on_htmx(render(request, template, context))


BULK_MAX = 500


def _bulk_step(do: str, text: str):
    """(change, permission check, past-tense word) for a common reply or close."""
    if do == "reply":
        return (
            lambda actor, pk: query_logic.reply_or_update(actor, pk, text),
            can_reply,
            "Replied to",
        )
    return (lambda actor, pk: query_logic.close_query(actor, pk, text), can_verify_close, "Closed")


def _plural(count: int) -> str:
    return f"{count:,} quer{'y' if count == 1 else 'ies'}"


@require_POST
def query_bulk(request):
    """Common reply (or update) / common close for many ticked queries, or for every query
    matching the list's search and filters. Each query keeps its own rules; the ones that
    can't be changed are skipped and named in the message."""
    company = request.company
    branches = visible_branches(request.user, company)
    if company is None or not branches.exists():
        return forbidden(request, "You need access to a Branch of this company to see its queries.")
    # The list's search and column filters, as they were when the user ticked.
    table = _query_table(branches, QueryDict(request.POST.get("filters", "")))
    list_url = f"{reverse('audits:query_list')}?{table.filter_query}"
    do = request.POST.get("do", "")
    text = request.POST.get("text", "").strip()
    if do not in ("reply", "close"):
        logger.warning("Bulk query step %r refused for %s", do, request.user.username)
        messages.error(request, "Choose Reply or Close.")
        return redirect(list_url)
    if do == "reply" and not text:
        logger.info("Bulk reply by %s refused: no text", request.user.username)
        messages.error(request, "Type the common reply first. Nothing was changed.")
        return redirect(list_url)
    pickable = _pickable(table, _bulk_rights(request.user, company))
    if request.POST.get("all_matching") == "1":
        picked = pickable.exclude(pk__in=_posted_ids(request, "exclude"))
    else:
        picked = pickable.filter(pk__in=_posted_ids(request, "ids"))
    picked = list(picked.select_related("audit__branch", "line").order_by("pk")[: BULK_MAX + 1])
    if len(picked) > BULK_MAX:
        logger.info("Bulk %s by %s refused: over %s queries", do, request.user.username, BULK_MAX)
        messages.error(
            request,
            f"At most {BULK_MAX:,} queries can be changed at once. "
            "Narrow the list with a filter and try again. Nothing was changed.",
        )
        return redirect(list_url)
    expected = request.POST.get("expected", "")
    if expected.isdecimal() and len(picked) > int(expected):
        # More queries match than the user confirmed (e.g. new ones were raised meanwhile).
        logger.info("Bulk %s refused: %s matching, %s confirmed", do, len(picked), expected)
        messages.error(
            request,
            f"The list changed: {len(picked):,} queries now match, not {int(expected):,}. "
            "Nothing was changed. Check the list and try again.",
        )
        return redirect(list_url)
    change, allowed, done_word = _bulk_step(do, text)
    done, skipped = query_logic.bulk_change(request.user, picked, change, allowed)
    logger.info(
        "%s bulk %s: %s done, %s skipped", request.user.username, do, len(done), len(skipped)
    )
    if done:
        messages.success(request, f"{done_word} {_plural(len(done))}.")
    if skipped:
        shown = "; ".join(f"{query.line.item_code}: {reason}" for query, reason in skipped[:10])
        more = f" … and {len(skipped) - 10:,} more" if len(skipped) > 10 else ""
        messages.warning(request, f"{_plural(len(skipped))} skipped — {shown}{more}")
    if not done and not skipped:
        messages.info(request, "No open queries were ticked, so nothing changed.")
    return redirect(list_url)


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
    can_reopen = can_reopen_query(user, query)
    reopen_frozen = query.is_closed and query.audit.is_locked
    show_close_tab = can_close_right and (not query.is_closed or can_reopen or reopen_frozen)
    active_tab = request.GET.get("tab", "reply")
    if active_tab not in QUERY_TABS or (active_tab == "close" and not show_close_tab):
        active_tab = "reply"
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
        "can_reopen": can_reopen,
        "reopen_frozen": reopen_frozen,
        "show_close_tab": show_close_tab,
        "owners": query_logic.owner_choices(query) if can_add_action(user, query) else [],
        "today": timezone.localdate(),
        "active_tab": active_tab,
    }
    return render(request, "audits/query_detail.html", context)


QUERY_TABS = ("reply", "media", "actions", "close", "history")


def _query_page(query_pk: int, tab: str):
    return redirect(f"{reverse('audits:query_detail', args=[query_pk])}?tab={tab}")


def _run(request, query_pk, change, success: str, tab: str = "reply"):
    """Run one change from audits.queries; show its message and go back to the query page,
    on the tab where the change was made."""
    try:
        change()
    except query_logic.QueryError as error:
        logger.info("Query %s change refused for %s: %s", query_pk, request.user.username, error)
        messages.error(request, str(error))
    else:
        messages.success(request, success)
    return _query_page(query_pk, tab)


@require_POST
def query_reply(request, pk):
    query = _visible_query(request, pk)
    if not can_reply(request.user, query):
        return forbidden(request, NO_REPLY_RIGHT)
    text = request.POST.get("text", "")
    success = "Update added." if getattr(query, "reply", None) else "Reply submitted."
    change = lambda: query_logic.reply_or_update(request.user, query.pk, text)
    return _run(request, query.pk, change, success)


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
        return _query_page(query.pk, "actions")
    owner_id = request.POST.get("owner", "")
    owner_id = int(owner_id) if owner_id.isdecimal() and owner_id.isascii() else None
    change = lambda: query_logic.add_action(
        request.user,
        query.pk,
        request.POST.get("description", ""),
        owner_id,
        form.cleaned_data["due_date"],
    )
    return _run(request, query.pk, change, "Action added.", tab="actions")


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
    return _run(request, query.pk, lambda: run(request.user, action.pk), success, tab="actions")


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
        tab="close",
    )


@require_POST
def query_reopen(request, pk):
    query = _visible_query(request, pk)
    if not can_reopen_query(request.user, query):
        return forbidden(
            request,
            NO_VERIFY_RIGHT
            + " A Closed query of a Locked audit can't be reopened until Admin reopens the audit.",
        )
    reason = request.POST.get("reason", "")
    return _run(
        request,
        query.pk,
        lambda: query_logic.reopen_query(request.user, query.pk, reason),
        "Query reopened. It is Open again.",
        tab="close",
    )
