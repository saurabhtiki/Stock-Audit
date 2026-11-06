"""Phase 4: query replies, actions, close / reopen, carry forward, item history, pending items."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.rights import Role
from audits import queries as query_logic
from audits.models import Action, Audit, AuditLine, Item, Query, QueryReply, QueryUpdate
from core.models import ActivityLog

from .conftest import client_for, make_user
from .test_audits import audit, grant, manager, priya  # noqa: F401
from .test_counting import anil, count, frozen  # noqa: F401

QS = Query.Status
AS = Action.Status


# ---------- fixtures ----------


@pytest.fixture
def raised(frozen, anil):  # noqa: F811
    """Audit Aug26 at Checking Done with one Shortage query on A100 (ERP 100, counted 90)."""
    client = client_for(anil)
    count(client, frozen, "A100", "90")
    count(client, frozen, "A200", "6")  # OK, no query
    client.post(reverse("audits:checking_done", args=[frozen.pk]), {"confirm": "on"})
    frozen.refresh_from_db()
    assert frozen.stage == Audit.Stage.CHECKING_DONE
    return Query.objects.get()


@pytest.fixture
def replier(branches, company_a):
    """User with the Reply right in North."""
    user = make_user("rita")
    grant(user, branches[0], company_a, role=Role.USER, reply_queries=True)
    return user


@pytest.fixture
def boss(branches, company_a):
    """Manager with Actions and Verify & close rights in North."""
    user = make_user("vikram")
    grant(
        user,
        branches[0],
        company_a,
        role=Role.MANAGER,
        manage_actions=True,
        verify_close_queries=True,
    )
    return user


@pytest.fixture
def doer(branches, company_a):
    """A store keeper who only views North; owns actions."""
    user = make_user("deepa")
    grant(user, branches[0], company_a, role=Role.VIEWER, view_reports=True)
    return user


def url(name, *args):
    return reverse(f"audits:{name}", args=args)


def reply(client, query, text="Two cartons sent to Branch B."):
    return client.post(url("query_reply", query.pk), {"text": text})


def add_action(client, query, owner, due=""):
    return client.post(
        url("action_add", query.pk),
        {"description": "Raise transfer slip", "owner": owner.pk, "due_date": due},
    )


def status_of(query):
    query.refresh_from_db()
    return query.status


# ---------- full status flow ----------


def test_full_flow_open_to_closed(raised, replier, boss, doer):
    assert raised.status == QS.OPEN
    reply(client_for(replier), raised)
    assert status_of(raised) == QS.REPLY_SUBMITTED

    boss_client = client_for(boss)
    add_action(boss_client, raised, doer)
    assert status_of(raised) == QS.ACTION_PENDING
    action = Action.objects.get()
    assert action.owner == doer and action.status == AS.OPEN

    # blocked while the action isn't Verified
    boss_client.post(url("query_close", raised.pk), {"note": ""})
    assert status_of(raised) == QS.ACTION_PENDING

    # the owner (no Actions right) can mark their own action Done
    client_for(doer).post(url("action_step", action.pk, "done"))
    action.refresh_from_db()
    assert action.status == AS.DONE and action.done_by == doer

    boss_client.post(url("action_step", action.pk, "verify"))
    action.refresh_from_db()
    assert action.status == AS.VERIFIED and action.verified_by == boss

    boss_client.post(url("query_close", raised.pk), {"note": ""})
    raised.refresh_from_db()
    assert raised.status == QS.CLOSED and raised.closed_by == boss
    for name in ("reply query", "add action", "action done", "action verified", "close query"):
        assert ActivityLog.objects.filter(action=name).exists(), name


def test_verifier_may_verify_own_done_action(raised, replier, boss):
    reply(client_for(replier), raised)
    client = client_for(boss)
    add_action(client, raised, boss)
    action = Action.objects.get()
    client.post(url("action_step", action.pk, "done"))
    client.post(url("action_step", action.pk, "verify"))
    action.refresh_from_db()
    assert action.status == AS.VERIFIED


def test_undo_done_until_verified(raised, replier, boss, doer):
    reply(client_for(replier), raised)
    add_action(client_for(boss), raised, doer)
    action = Action.objects.get()
    doer_client = client_for(doer)
    doer_client.post(url("action_step", action.pk, "done"))
    doer_client.post(url("action_step", action.pk, "undo"))
    action.refresh_from_db()
    assert action.status == AS.OPEN and action.done_by is None
    doer_client.post(url("action_step", action.pk, "done"))
    client_for(boss).post(url("action_step", action.pk, "verify"))
    doer_client.post(url("action_step", action.pk, "undo"))
    action.refresh_from_db()
    assert action.status == AS.VERIFIED


def test_verify_needs_done_first(raised, replier, boss, doer):
    reply(client_for(replier), raised)
    add_action(client_for(boss), raised, doer)
    action = Action.objects.get()
    client_for(boss).post(url("action_step", action.pk, "verify"))
    action.refresh_from_db()
    assert action.status == AS.OPEN


# ---------- close rules ----------


def test_close_without_actions_needs_note(raised, replier, boss):
    reply(client_for(replier), raised)
    client = client_for(boss)
    response = client.post(url("query_close", raised.pk), {"note": "  "}, follow=True)
    assert "short note" in response.content.decode()
    assert status_of(raised) == QS.REPLY_SUBMITTED
    client.post(url("query_close", raised.pk), {"note": "Counting error; recount matched."})
    raised.refresh_from_db()
    assert raised.status == QS.CLOSED and raised.close_note.startswith("Counting error")


def test_cannot_close_before_reply(raised, boss):
    client_for(boss).post(url("query_close", raised.pk), {"note": "nothing"})
    assert status_of(raised) == QS.OPEN


def test_no_actions_before_reply(raised, boss, doer):
    response = add_action(client_for(boss), raised, doer)
    assert response.status_code == 403
    assert not Action.objects.exists()


def test_action_owner_must_be_company_user(raised, replier, boss, company_b):
    reply(client_for(replier), raised)
    outsider = make_user("outsider")
    grant(outsider, company_b.branches.create(name="East", code="E1"), company_b)
    add_action(client_for(boss), raised, outsider)
    assert not Action.objects.exists()
    assert status_of(raised) == QS.REPLY_SUBMITTED


def test_bad_due_date_refused(raised, replier, boss, doer):
    reply(client_for(replier), raised)
    add_action(client_for(boss), raised, doer, due="not-a-date")
    assert not Action.objects.exists()


# ---------- reopen ----------


def test_reopen_needs_reason_and_right(raised, replier, boss):
    reply(client_for(replier), raised)
    boss_client = client_for(boss)
    boss_client.post(url("query_close", raised.pk), {"note": "No action needed."})
    assert status_of(raised) == QS.CLOSED

    assert (
        client_for(replier).post(url("query_reopen", raised.pk), {"reason": "x"}).status_code == 403
    )
    boss_client.post(url("query_reopen", raised.pk), {"reason": ""})
    assert status_of(raised) == QS.CLOSED

    boss_client.post(url("query_reopen", raised.pk), {"reason": "Stock still short at recheck."})
    raised.refresh_from_db()
    assert raised.status == QS.OPEN and raised.closed_at is None
    entry = ActivityLog.objects.get(action="reopen query")
    assert entry.changes["reason"][1] == "Stock still short at recheck."

    # an update on the reopened query counts as the new reply
    reply(client_for(replier), raised, "Recounted: 8 pcs in bin C4.")
    assert status_of(raised) == QS.REPLY_SUBMITTED
    assert QueryUpdate.objects.count() == 1 and QueryReply.objects.count() == 1


# ---------- reply and updates ----------


def test_one_reply_then_updates(raised, replier):
    client = client_for(replier)
    reply(client, raised, "First reply")
    reply(client, raised, "Later news")
    assert QueryReply.objects.get().text == "First reply"
    assert QueryUpdate.objects.get().text == "Later news"


def test_only_writer_edits_reply_until_closed(raised, replier, boss, branches, company_a):
    reply(client_for(replier), raised, "First reply")
    other = make_user("other")
    grant(other, branches[0], company_a, reply_queries=True)
    assert (
        client_for(other).post(url("query_reply_edit", raised.pk), {"text": "hacked"}).status_code
        == 403
    )
    client_for(replier).post(url("query_reply_edit", raised.pk), {"text": "Better reply"})
    assert QueryReply.objects.get().text == "Better reply"
    assert ActivityLog.objects.filter(action="edit reply").exists()

    client_for(boss).post(url("query_close", raised.pk), {"note": "ok"})
    assert (
        client_for(replier).post(url("query_reply_edit", raised.pk), {"text": "late"}).status_code
        == 403
    )
    assert QueryReply.objects.get().text == "Better reply"


def test_empty_reply_refused(raised, replier):
    reply(client_for(replier), raised, "   ")
    assert not QueryReply.objects.exists()
    assert status_of(raised) == QS.OPEN


# ---------- rights and visibility ----------


def test_rights_are_checked(raised, doer, boss, anil, replier):  # noqa: F811
    assert reply(client_for(doer), raised).status_code == 403  # viewer
    assert reply(client_for(anil), raised).status_code == 403  # auditor, no Reply right
    reply(client_for(replier), raised)
    assert add_action(client_for(replier), raised, doer).status_code == 403  # no Actions right
    assert client_for(replier).post(url("query_close", raised.pk), {"note": "x"}).status_code == 403


def test_viewer_sees_query_without_buttons(raised, doer):
    page = client_for(doer).get(url("query_detail", raised.pk)).content.decode()
    assert "A100" in page and "Item history" in page
    assert "Submit reply" not in page and "Add action" not in page and "Close query" not in page


def test_other_branch_user_cannot_see_query(raised, branches, company_a):
    south_user = make_user("south")
    grant(south_user, branches[1], company_a, reply_queries=True)
    client = client_for(south_user)
    assert client.get(url("query_detail", raised.pk)).status_code == 404
    assert reply(client, raised).status_code == 404
    listing = client.get(url("query_list")).content.decode()
    assert "A100" not in listing


def test_query_list_and_csv(raised, boss):
    client = client_for(boss)
    page = client.get(url("query_list")).content.decode()
    assert "A100" in page and "Open" in page
    assert "A100" not in client.get(url("query_list") + "?status=closed").content.decode()
    csv = b"".join(client.get(url("query_list") + "?export=csv").streaming_content).decode()
    assert "A100" in csv


# ---------- carry forward and item history ----------


def _next_audit(previous, manager_user, physical):
    """A later audit of the same Branch where A100 is counted again."""
    later = Audit.objects.create(
        branch=previous.branch,
        title="Sep26",
        audit_date=previous.audit_date + timedelta(days=30),
        stock_as_on=previous.audit_date + timedelta(days=29),
        stage=Audit.Stage.ERP_FROZEN,
        created_by=manager_user,
    )
    item = Item.objects.get(code="A100")
    AuditLine.objects.create(
        audit=later,
        item=item,
        item_code="A100",
        description=item.description,
        uom="PCS",
        erp_qty=Decimal(100),
        rate=Decimal("2.50"),
        erp_value=Decimal(250),
        physical_qty=Decimal(physical),
        variance_qty=Decimal(physical) - 100,
        count_status=AuditLine.Status.SHORTAGE,
    )
    return later


def test_open_query_carries_forward_without_copy(raised, boss, manager):  # noqa: F811
    later = _next_audit(raised.audit, manager, 95)
    assert list(query_logic.queries_for_audit(later)) == [raised]
    page = client_for(boss).get(url("audit_detail", later.pk)).content.decode()
    assert "Carried" in page
    assert Query.objects.count() == 1


def test_closed_query_not_carried(raised, replier, boss, manager):  # noqa: F811
    reply(client_for(replier), raised)
    client_for(boss).post(url("query_close", raised.pk), {"note": "Fine."})
    later = _next_audit(raised.audit, manager, 95)
    assert list(query_logic.queries_for_audit(later)) == []
    # the original audit still lists its own closed query
    assert list(query_logic.queries_for_audit(raised.audit)) == [raised]


def test_item_history_one_row_per_audit(raised, manager, boss):  # noqa: F811
    later = _next_audit(raised.audit, manager, 95)
    history = query_logic.item_history(raised)
    assert [row.audit_id for row in history] == [raised.audit_id, later.pk]
    assert history[0].query_status == "Open" and history[1].query_status == ""
    page = client_for(boss).get(url("query_detail", raised.pk)).content.decode()
    assert "Sep26" in page


# ---------- overdue and pending items ----------


def test_overdue_flag(raised, replier, boss, doer):
    reply(client_for(replier), raised)
    add_action(client_for(boss), raised, doer)
    action = Action.objects.get()
    assert not action.is_overdue  # no due date
    action.due_date = timezone.localdate() - timedelta(days=1)
    action.save()
    assert action.is_overdue
    listing = client_for(boss).get(url("query_list") + "?overdue_actions_min=1").content.decode()
    assert "A100" in listing
    action.status = AS.VERIFIED
    assert not action.is_overdue


def test_pending_items_per_user(raised, replier, boss, doer, company_a):
    assert list(query_logic.pending_for(replier, company_a)["to_reply"]) == [raised]
    assert list(query_logic.pending_for(doer, company_a)["to_reply"]) == []
    reply(client_for(replier), raised)
    assert list(query_logic.pending_for(boss, company_a)["to_close"]) == [raised]

    add_action(client_for(boss), raised, doer)
    action = Action.objects.get()
    assert list(query_logic.pending_for(doer, company_a)["my_actions"]) == [action]
    assert list(query_logic.pending_for(boss, company_a)["to_close"]) == []
    client_for(doer).post(url("action_step", action.pk, "done"))
    assert list(query_logic.pending_for(boss, company_a)["to_verify"]) == [action]
    assert list(query_logic.pending_for(doer, company_a)["my_actions"]) == []

    home = client_for(boss).get(reverse("core:home")).content.decode()
    assert "Actions to verify" in home


def test_queries_never_deleted(raised):
    with pytest.raises(ValidationError):
        raised.delete()
