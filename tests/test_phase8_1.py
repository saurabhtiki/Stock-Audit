"""Phase 8.1: query under its line in the PDF, one tab look on the Query page, no Branch list
on Home."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from audits.models import Action, AuditLine, Query, QueryUpdate

from .conftest import client_for
from .test_audits import audit, manager, priya  # noqa: F401
from .test_counting import anil, frozen  # noqa: F401
from .test_lock_media_reports import locker, pdf_text  # noqa: F401
from .test_queries import add_action, boss, doer, raised, replier, reply  # noqa: F401


def pdf_of(user, query) -> str:
    return pdf_text(client_for(user).get(reverse("audits:audit_pdf", args=[query.audit_id])))


def test_pdf_query_sits_under_its_line(raised, replier, boss, doer, locker):  # noqa: F811
    reply(client_for(replier), raised, "Two cartons sent to Branch B.")
    client_for(replier).post(
        reverse("audits:query_reply", args=[raised.pk]), {"text": "Slip raised today."}
    )
    past = (timezone.localdate() - timedelta(days=1)).isoformat()
    add_action(client_for(boss), raised, doer)
    Action.objects.update(due_date=past)
    assert QueryUpdate.objects.count() == 1

    text = pdf_of(locker, raised)
    assert "Queries and actions" not in text  # old separate table is gone
    assert "Query: Action Pending" in text
    assert "Two cartons sent to Branch B." in text and "Slip raised today." in text
    assert "Raise transfer slip" in text and r"\(overdue\)" in text
    assert "Not closed / Closed" in text  # count summary line
    assert "Other queries" not in text


def test_pdf_query_without_reply_and_closed_note(raised, locker):  # noqa: F811
    text = pdf_of(locker, raised)
    assert "Query: Open" in text and "No reply yet" in text

    Query.objects.filter(pk=raised.pk).update(
        status=Query.Status.CLOSED,
        close_note="Recount matched",
        closed_by=locker,
        closed_at=timezone.now(),
    )
    text = pdf_of(locker, raised)
    assert "Query: Closed" in text and "Recount matched" in text


def test_pdf_query_on_ok_line_goes_to_other_queries(raised, locker):  # noqa: F811
    AuditLine.objects.filter(pk=raised.line_id).update(count_status=AuditLine.Status.OK)
    text = pdf_of(locker, raised)
    assert r"Other queries \(1\)" in text and "Query: Open" in text


def test_query_page_uses_app_tabs(raised, boss):  # noqa: F811
    page = client_for(boss).get(reverse("audits:query_detail", args=[raised.pk])).content.decode()
    assert "tabs app-tabs" in page and "tabs-lift" not in page


def test_home_has_no_branch_list(priya, manager):  # noqa: F811
    page = client_for(manager).get(reverse("core:home")).content.decode()
    assert "Your Branches" not in page and "Go to audits" in page
