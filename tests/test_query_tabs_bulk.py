"""Query page tabs, and common reply / close for many queries from the Queries list."""

import pytest
from django.urls import reverse

from audits.models import Audit, Query, QueryReply, QueryUpdate
from core.models import ActivityLog

from .conftest import client_for
from .test_audits import audit, grant, manager, priya  # noqa: F401
from .test_counting import anil, count, frozen  # noqa: F401
from .test_queries import add_action, boss, doer, raised, replier, reply  # noqa: F401

QS = Query.Status


def url(name, *args):
    return reverse(f"audits:{name}", args=args)


@pytest.fixture
def two_raised(frozen, anil):  # noqa: F811
    """Audit Aug26 at Checking Done with queries on A100 (Shortage) and A200 (Excess)."""
    client = client_for(anil)
    count(client, frozen, "A100", "90")
    count(client, frozen, "A200", "20")
    count(client, frozen, "A300", "0")
    client.post(reverse("audits:checking_done", args=[frozen.pk]), {"confirm": "on"})
    frozen.refresh_from_db()
    assert frozen.stage == Audit.Stage.CHECKING_DONE
    return {query.line.item_code: query for query in Query.objects.select_related("line")}


def bulk(client, do, text="Recount matched the ERP.", ids=(), follow=True, **extra):
    data = {"do": do, "text": text, "ids": [str(pk) for pk in ids], **extra}
    return client.post(url("query_bulk"), data, follow=follow)


def status_of(query):
    query.refresh_from_db()
    return query.status


# ---------- tabs on the query page ----------


def test_query_page_shows_tabs_and_opens_the_asked_tab(raised, boss):  # noqa: F811
    client = client_for(boss)
    page = client.get(url("query_detail", raised.pk)).content.decode()
    for label in ("Reply", "Photos and files", "Actions", "Close query", "Item history"):
        assert f'aria-label="{label}"' in page
    assert 'aria-label="Reply" checked' in page

    page = client.get(url("query_detail", raised.pk) + "?tab=history").content.decode()
    assert 'aria-label="Item history" checked' in page
    assert 'aria-label="Reply" checked' not in page


def test_change_returns_to_its_tab(raised, replier, boss, doer):  # noqa: F811
    reply(client_for(replier), raised)
    response = add_action(client_for(boss), raised, doer)
    assert response["Location"].endswith("?tab=actions")
    response = client_for(boss).post(url("query_close", raised.pk), {"note": ""})
    assert response["Location"].endswith("?tab=close")


def test_close_tab_hidden_without_right_and_falls_back(raised, replier):  # noqa: F811
    page = client_for(replier).get(url("query_detail", raised.pk) + "?tab=close").content.decode()
    assert 'aria-label="Close query"' not in page
    assert 'aria-label="Reply" checked' in page


def test_unknown_tab_opens_reply(raised, boss):  # noqa: F811
    page = client_for(boss).get(url("query_detail", raised.pk) + "?tab=evil").content.decode()
    assert 'aria-label="Reply" checked' in page


# ---------- list tick boxes ----------


def test_list_has_tick_boxes_only_for_workers(two_raised, boss, doer):  # noqa: F811
    page = client_for(boss).get(url("query_list")).content.decode()
    assert page.count("data-pick-id=") == 2 and 'data-matching="2"' in page
    # The step is sent in a hidden field, since the busy (disabled) button isn't posted.
    assert 'name="do" x-ref="bulk_step"' in page
    assert "{#" not in page  # a template comment split over lines prints as text
    assert 'value="close"' in page and 'value="reply"' not in page  # boss: Verify right only

    viewer_page = client_for(doer).get(url("query_list")).content.decode()
    assert "data-pick-id=" not in viewer_page and "query_bulk" not in viewer_page


def test_closed_query_cannot_be_ticked(two_raised, replier, boss):  # noqa: F811
    a200 = two_raised["A200"]
    reply(client_for(replier), a200)
    client = client_for(boss)
    client.post(url("query_close", a200.pk), {"note": "Recount matched."})
    assert status_of(a200) == QS.CLOSED
    page = client.get(url("query_list")).content.decode()
    assert page.count("data-pick-id=") == 1 and 'data-matching="1"' in page


# ---------- common reply ----------


def test_bulk_reply_gives_reply_or_update(two_raised, replier):  # noqa: F811
    a100, a200 = two_raised["A100"], two_raised["A200"]
    client = client_for(replier)
    reply(client, a100, "First reply")
    response = bulk(client, "reply", "Stock moved to Branch B.", ids=[a100.pk, a200.pk])
    page = response.content.decode()
    assert "Replied to 2 queries." in page
    assert QueryUpdate.objects.get(query=a100).text == "Stock moved to Branch B."
    assert QueryReply.objects.get(query=a200).text == "Stock moved to Branch B."
    assert status_of(a200) == QS.REPLY_SUBMITTED
    assert ActivityLog.objects.filter(action="reply query").count() == 2  # A100 first + A200


def test_bulk_reply_needs_text(two_raised, replier):  # noqa: F811
    page = bulk(client_for(replier), "reply", "   ", ids=[q.pk for q in two_raised.values()])
    assert "Type the common reply first" in page.content.decode()
    assert not QueryReply.objects.exists()


def test_bulk_reply_all_matching_filter(two_raised, replier):  # noqa: F811
    response = bulk(
        client_for(replier),
        "reply",
        ids=[],
        filters="q=A100",
        all_matching="1",
        expected="1",
    )
    assert "Replied to 1 query." in response.content.decode()
    assert QueryReply.objects.get().query == two_raised["A100"]
    assert response.redirect_chain[-1][0].endswith("?q=A100")


def test_bulk_all_matching_with_exclude(two_raised, replier):  # noqa: F811
    bulk(
        client_for(replier),
        "reply",
        all_matching="1",
        expected="1",
        exclude=[str(two_raised["A200"].pk)],
    )
    assert QueryReply.objects.get().query == two_raised["A100"]


def test_bulk_refuses_when_list_grew(two_raised, replier):  # noqa: F811
    response = bulk(client_for(replier), "reply", all_matching="1", expected="1")
    assert "The list changed: 2 queries now match, not 1" in response.content.decode()
    assert not QueryReply.objects.exists()


def test_bulk_reply_without_right_changes_nothing(two_raised, boss, doer):  # noqa: F811
    ids = [query.pk for query in two_raised.values()]
    page = bulk(client_for(boss), "reply", ids=ids).content.decode()  # boss: verify only
    assert "2 queries skipped" in page and "don&#x27;t have the right" in page
    bulk(client_for(doer), "reply", ids=ids)  # viewer: nothing pickable
    assert not QueryReply.objects.exists()


def test_bulk_ignores_other_company_ids(two_raised, replier, company_b):  # noqa: F811
    page = bulk(client_for(replier), "reply", ids=[999999]).content.decode()
    assert "nothing changed" in page
    assert not QueryReply.objects.exists()


# ---------- common close ----------


def test_bulk_close_closes_ready_and_skips_others(two_raised, replier, boss):  # noqa: F811
    a100, a200 = two_raised["A100"], two_raised["A200"]
    reply(client_for(replier), a100)
    response = bulk(client_for(boss), "close", "Counting error.", ids=[a100.pk, a200.pk])
    page = response.content.decode()
    assert "Closed 1 query." in page
    assert "1 query skipped" in page and "A200" in page and "only after a reply" in page
    a100.refresh_from_db()
    assert (
        a100.status == QS.CLOSED and a100.close_note == "Counting error." and a100.closed_by == boss
    )
    assert status_of(a200) == QS.OPEN


def test_bulk_close_without_note_skips_query_with_no_actions(two_raised, replier, boss):  # noqa: F811
    a100 = two_raised["A100"]
    reply(client_for(replier), a100)
    page = bulk(client_for(boss), "close", "", ids=[a100.pk]).content.decode()
    assert "Write a short note" in page
    assert status_of(a100) == QS.REPLY_SUBMITTED


def test_bulk_unknown_step(two_raised, boss):  # noqa: F811
    page = bulk(client_for(boss), "delete", ids=[two_raised["A100"].pk]).content.decode()
    assert "Choose Reply or Close." in page


def test_bulk_needs_post(boss):  # noqa: F811
    assert client_for(boss).get(url("query_bulk")).status_code == 405


# ---------- the Items list still uses the shared tick-box script ----------


def test_items_page_uses_shared_script(boss):  # noqa: F811
    page = client_for(boss).get(url("item_list")).content.decode()
    assert "bulk_select.js" in page and "bulkSelect(" in page
