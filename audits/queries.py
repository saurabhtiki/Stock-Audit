"""Query replies and actions: the status rules live here, views only call these.

Query:  Open → (reply) Reply Submitted → (first action) Action Pending → (close) Closed.
        Close needs every action Verified; with no actions, a short note instead.
        Reopen sends a Closed query back to Open (reason required).
Action: Open → Done → Verified. Done can be undone until Verified.
"""

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from audits.models import Action, Audit, AuditLine, Query, QueryReply, QueryUpdate
from audits.permissions import branches_with_right
from core.activity import log_activity

logger = logging.getLogger(__name__)

QS = Query.Status
AS = Action.Status
MAX_TEXT = 2000


class QueryError(Exception):
    """A rule stopped the change; the message is shown to the user."""


def _clean_text(text: str, what: str, max_length: int = MAX_TEXT) -> str:
    text = (text or "").strip()
    if not text:
        raise QueryError(f"Type the {what} first.")
    if len(text) > max_length:
        raise QueryError(f"The {what} can be at most {max_length:,} characters.")
    return text


def _locked_query(query_pk: int) -> Query:
    return Query.objects.select_for_update().select_related("audit__branch").get(pk=query_pk)


def _set_status(actor, query: Query, new_status: str, action: str, extra: dict | None = None):
    old = query.status
    query.status = new_status
    query.save(update_fields=["status", "closed_by", "closed_at", "close_note"])
    changes = {"status": [old, new_status]} if old != new_status else {}
    log_activity(actor, action, query, {**changes, **(extra or {})})


# ---------- Reply and updates ----------


@transaction.atomic
def submit_reply(actor, query_pk: int, text: str) -> QueryReply:
    query = _locked_query(query_pk)
    text = _clean_text(text, "reply")
    if query.is_closed:
        raise QueryError("This query is Closed. Reopen it first.")
    if QueryReply.objects.filter(query=query).exists():
        raise QueryError("This query already has a reply. Add an update instead.")
    reply = QueryReply.objects.create(query=query, text=text, created_by=actor)
    _set_status(actor, query, QS.REPLY_SUBMITTED, "reply query", {"reply": [None, text]})
    return reply


@transaction.atomic
def edit_reply(actor, query_pk: int, text: str) -> QueryReply:
    query = _locked_query(query_pk)
    text = _clean_text(text, "reply")
    reply = QueryReply.objects.filter(query=query).first()
    if reply is None or reply.created_by_id != actor.pk:
        raise QueryError("Only the person who wrote the reply can change it.")
    if query.is_closed:
        raise QueryError("This query is Closed, so the reply can't be changed.")
    if reply.text != text:
        old = reply.text
        reply.text = text
        reply.edited_at = timezone.now()
        reply.save(update_fields=["text", "edited_at"])
        log_activity(actor, "edit reply", query, {"reply": [old, text]})
    return reply


@transaction.atomic
def add_update(actor, query_pk: int, text: str) -> QueryUpdate:
    """A dated note under the reply. On a reopened (Open) query it counts as the new reply."""
    query = _locked_query(query_pk)
    text = _clean_text(text, "update")
    if query.is_closed:
        raise QueryError("This query is Closed. Reopen it first.")
    if not QueryReply.objects.filter(query=query).exists():
        raise QueryError("Write the reply first; updates go under it.")
    update = QueryUpdate.objects.create(query=query, text=text, created_by=actor)
    new_status = QS.REPLY_SUBMITTED if query.status == QS.OPEN else query.status
    _set_status(actor, query, new_status, "add query update", {"update": [None, text]})
    return update


# ---------- Actions ----------


def owner_choices(query: Query):
    """Active users who can work in the query's company (members, plus group Admins)."""
    company = query.audit.branch.company
    return (
        User.objects.filter(is_active=True)
        .filter(Q(is_admin=True) | Q(memberships__company=company, memberships__is_active=True))
        .distinct()
        .order_by("full_name", "username")
    )


@transaction.atomic
def add_action(actor, query_pk: int, description: str, owner_id, due_date=None) -> Action:
    query = _locked_query(query_pk)
    description = _clean_text(description, "action", max_length=1000)
    if query.status not in (QS.REPLY_SUBMITTED, QS.ACTION_PENDING):
        raise QueryError("Actions can be added only after a reply, and not to a Closed query.")
    owner = owner_choices(query).filter(pk=owner_id).first() if owner_id else None
    if owner is None:
        raise QueryError("Choose who will do this action.")
    action = Action.objects.create(
        query=query, description=description, owner=owner, due_date=due_date, created_by=actor
    )
    log_activity(
        actor,
        "add action",
        action,
        {
            "description": [None, description],
            "owner": [None, owner.username],
            "due_date": [None, due_date.isoformat() if due_date else None],
        },
    )
    if query.status != QS.ACTION_PENDING:
        _set_status(actor, query, QS.ACTION_PENDING, "query action pending")
    return action


def _locked_action(action_pk: int) -> Action:
    action = (
        Action.objects.select_for_update().select_related("query__audit__branch").get(pk=action_pk)
    )
    if action.query.is_closed:
        raise QueryError("This query is Closed. Reopen it first.")
    return action


def _move_action(actor, action: Action, new_status: str, log_name: str, fields: list[str]):
    old = action.status
    action.status = new_status
    action.save(update_fields=["status", *fields])
    log_activity(actor, log_name, action, {"status": [old, new_status]})


@transaction.atomic
def mark_done(actor, action_pk: int) -> Action:
    action = _locked_action(action_pk)
    if action.status != AS.OPEN:
        raise QueryError("Only an Open action can be marked Done.")
    action.done_by, action.done_at = actor, timezone.now()
    _move_action(actor, action, AS.DONE, "action done", ["done_by", "done_at"])
    return action


@transaction.atomic
def undo_done(actor, action_pk: int) -> Action:
    action = _locked_action(action_pk)
    if action.status != AS.DONE:
        raise QueryError("Only a Done action (not yet Verified) can be set back to Open.")
    action.done_by, action.done_at = None, None
    _move_action(actor, action, AS.OPEN, "action undone", ["done_by", "done_at"])
    return action


@transaction.atomic
def verify_action(actor, action_pk: int) -> Action:
    action = _locked_action(action_pk)
    if action.status != AS.DONE:
        raise QueryError("An action must be marked Done before it can be Verified.")
    action.verified_by, action.verified_at = actor, timezone.now()
    _move_action(actor, action, AS.VERIFIED, "action verified", ["verified_by", "verified_at"])
    return action


# ---------- Close and reopen ----------


def close_blocker(query: Query) -> str:
    """Why the query can't be closed yet ('' = it can). Note rule is checked on submit."""
    if query.status == QS.CLOSED:
        return "This query is already Closed."
    if query.status == QS.OPEN:
        return "A query can be closed only after a reply."
    waiting = query.actions.exclude(status=AS.VERIFIED).count()
    if waiting:
        return f"{waiting} action{'s' if waiting != 1 else ''} not Verified yet."
    return ""


@transaction.atomic
def close_query(actor, query_pk: int, note: str = "") -> Query:
    query = _locked_query(query_pk)
    blocker = close_blocker(query)
    if blocker:
        raise QueryError(blocker)
    note = (note or "").strip()
    if len(note) > 1000:
        raise QueryError("The closing note can be at most 1,000 characters.")
    if not query.actions.exists() and not note:
        raise QueryError("No actions were added. Write a short note on why none is needed.")
    query.closed_by, query.closed_at, query.close_note = actor, timezone.now(), note
    _set_status(actor, query, QS.CLOSED, "close query", {"note": [None, note]} if note else None)
    return query


@transaction.atomic
def reopen_query(actor, query_pk: int, reason: str) -> Query:
    query = _locked_query(query_pk)
    reason = _clean_text(reason, "reason for reopening", max_length=500)
    if not query.is_closed:
        raise QueryError("Only a Closed query can be reopened.")
    query.closed_by, query.closed_at, query.close_note = None, None, ""
    _set_status(actor, query, QS.OPEN, "reopen query", {"reason": [None, reason]})
    return query


# ---------- Lists: carry forward, item history, pending items ----------


def queries_for_audit(audit: Audit):
    """This audit's queries, plus not-Closed queries carried from earlier audits of the Branch."""
    earlier = Q(audit__audit_date__lt=audit.audit_date) | Q(
        audit__audit_date=audit.audit_date, audit__pk__lt=audit.pk
    )
    carried = Q(audit__branch=audit.branch) & ~Q(status=QS.CLOSED) & earlier
    return (
        Query.objects.filter(Q(audit=audit) | carried)
        .select_related("audit", "line")
        .order_by("audit__audit_date", "line__item_code")
    )


def item_history(query: Query) -> list[AuditLine]:
    """One row per audit of this Branch that had the item, oldest first, with its query status."""
    lines = list(
        AuditLine.objects.filter(item=query.item, audit__branch=query.audit.branch)
        .exclude(audit__stage=Audit.Stage.CANCELLED)
        .select_related("audit")
        .order_by("audit__audit_date", "audit_id")
    )
    statuses = dict(Query.objects.filter(line__in=lines).values_list("line_id", "status"))
    labels = dict(QS.choices)
    for line in lines:
        status = statuses.get(line.pk)
        line.query_status = labels[status] if status else ""
    return lines


def pending_for(user, company) -> dict:
    """What waits for this user in the company: replies, own actions, verifications, closings."""
    live = Query.objects.filter(
        audit__stage=Audit.Stage.CHECKING_DONE, audit__branch__company=company
    )
    reply_branches = branches_with_right(user, company, "reply_queries")
    verify_branches = branches_with_right(user, company, "verify_close_queries")
    query_fields = ("audit__branch", "line")
    action_fields = ("query__audit__branch", "query__line", "owner")
    return {
        "to_reply": live.filter(status=QS.OPEN, audit__branch__in=reply_branches)
        .select_related(*query_fields)
        .order_by("created_at")[:50],
        "my_actions": Action.objects.filter(
            owner=user, status=AS.OPEN, query__in=live.exclude(status=QS.CLOSED)
        )
        .select_related(*action_fields)
        .order_by("due_date", "created_at")[:50],
        "to_verify": Action.objects.filter(
            status=AS.DONE,
            query__in=live.exclude(status=QS.CLOSED),
            query__audit__branch__in=verify_branches,
        )
        .select_related(*action_fields)
        .order_by("done_at")[:50],
        "to_close": live.filter(
            status__in=(QS.REPLY_SUBMITTED, QS.ACTION_PENDING), audit__branch__in=verify_branches
        )
        .exclude(actions__status__in=(AS.OPEN, AS.DONE))
        .select_related(*query_fields)
        .order_by("created_at")[:50],
    }
