"""Numbers behind the Dashboard tabs. Each function takes the shared filters and returns
plain rows, so the screen, the charts and the CSV all show the same figures.

Only Branches where the user has “View reports” are ever looked at (see `read_filters`).
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import (
    Case,
    CharField,
    Count,
    DateField,
    DecimalField,
    DurationField,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Abs, Coalesce, NullIf, TruncMonth
from django.utils import timezone

from audits.models import Action, Audit, AuditLine, Item, Query
from audits.permissions import QUERY_STAGES

ZERO = Decimal(0)
CENT = Decimal("0.01")


def _money(value) -> Decimal:
    """SQLite adds money up without its 2 decimals (10 instead of 10.00); put them back."""
    return Decimal(value or 0).quantize(CENT)


FLAGGED = (AuditLine.Status.EXCESS, AuditLine.Status.SHORTAGE)
# Ageing groups: (label, first day, last day); None = no upper end.
AGE_BUCKETS = (
    ("0–7", 0, 7),
    ("8–30", 8, 30),
    ("31–60", 31, 60),
    ("61–90", 61, 90),
    ("90+", 91, None),
)
DEFAULT_MONTHS = 12
FILTER_PARAMS = ("branch", "from", "to", "type", "item")
OVERDUE, NOT_LATE = "yes", "no"  # pending_actions().overdue
MAX_PICKED_ITEMS = 50  # more than this in the Item filter is ignored (keeps URLs short)


def _month_start(text: str) -> date | None:
    """'2026-04' → 1 Apr 2026; anything else → None."""
    try:
        year, month = (int(part) for part in text.split("-"))
        return date(year, month, 1)
    except (ValueError, AttributeError):
        return None


def _shift_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


@dataclass
class DashboardFilters:
    """The filter bar: Branches (subset of the allowed ones), month range, audit type and items."""

    allowed: list  # Branches the user may see on the dashboard
    branches: list
    month_from: date
    month_to: date  # first day of the last month shown
    audit_type: str = ""  # "" = all
    errors: list[str] = field(default_factory=list)
    items: list = field(default_factory=list)  # picked Items; empty = all items

    @property
    def item_ids(self) -> list[int]:
        return [item.pk for item in self.items]

    @property
    def date_to(self) -> date:
        last_day = calendar.monthrange(self.month_to.year, self.month_to.month)[1]
        return self.month_to.replace(day=last_day)

    @property
    def branch_ids(self) -> set[int]:
        return {branch.pk for branch in self.branches}

    def as_params(self) -> list[tuple[str, str]]:
        """The filters as URL pairs (only what differs from showing everything)."""
        pairs = [("from", f"{self.month_from:%Y-%m}"), ("to", f"{self.month_to:%Y-%m}")]
        if len(self.branches) != len(self.allowed):
            pairs += [("branch", str(branch.pk)) for branch in self.branches]
        if self.audit_type:
            pairs.append(("type", self.audit_type))
        pairs += [("item", str(item.pk)) for item in self.items]
        return pairs

    def audits(self, include_cancelled: bool = False):
        audits = Audit.objects.filter(
            branch__in=self.branches,
            audit_date__gte=self.month_from,
            audit_date__lte=self.date_to,
        )
        if self.audit_type:
            audits = audits.filter(audit_type=self.audit_type)
        if not include_cancelled:
            audits = audits.exclude(stage=Audit.Stage.CANCELLED)
        return audits

    def final_audits(self):
        """Audits whose counts are final (Checking Done or Locked)."""
        return self.audits().filter(stage__in=QUERY_STAGES)


def _read_items(params, company_id, errors: list[str]) -> list:
    """Picked Items (`item` = Item id, may repeat), only from this company."""
    wanted = list(dict.fromkeys(value for value in params.getlist("item") if value))
    if len(wanted) > MAX_PICKED_ITEMS:
        errors.append(f"Only the first {MAX_PICKED_ITEMS} picked items are used.")
        wanted = wanted[:MAX_PICKED_ITEMS]
    ids = [value for value in wanted if value.isdecimal()]
    found = Item.objects.filter(company_id=company_id, pk__in=ids) if ids else []
    by_id = {str(item.pk): item for item in found}
    if len(by_id) < len(wanted):
        errors.append("Some picked items were not found in this company, so they were left out.")
    return [by_id[value] for value in wanted if value in by_id]


def read_filters(params, allowed_branches, today: date | None = None) -> DashboardFilters:
    """Read the filter bar from the URL. Bad values fall back to the default with a message."""
    today = today or timezone.localdate()
    allowed = list(allowed_branches)
    errors = []

    picked_ids = {value for value in params.getlist("branch") if value.isdigit()}
    branches = [branch for branch in allowed if str(branch.pk) in picked_ids] or allowed
    if picked_ids - {str(branch.pk) for branch in allowed}:
        errors.append("Some picked Branches are not open to you, so they were left out.")

    default_to = today.replace(day=1)
    default_from = _shift_months(default_to, -(DEFAULT_MONTHS - 1))
    month_from = _month_start(params.get("from", "")) or default_from
    month_to = _month_start(params.get("to", "")) or default_to
    if params.get("from") and not _month_start(params["from"]):
        errors.append(f"“{params['from']}” is not a month like 2026-04, so the default is used.")
    if params.get("to") and not _month_start(params["to"]):
        errors.append(f"“{params['to']}” is not a month like 2026-04, so the default is used.")
    if month_from > month_to:
        errors.append("“From” month was after “To” month, so they were swapped.")
        month_from, month_to = month_to, month_from

    audit_type = params.get("type", "")
    if audit_type not in Audit.Type.values:
        audit_type = ""
    items = _read_items(params, allowed[0].company_id if allowed else None, errors)
    return DashboardFilters(allowed, branches, month_from, month_to, audit_type, errors, items)


# ---------- Audit status ----------


def status_rows(filters: DashboardFilters, include_cancelled: bool = False):
    """One row per audit: lines, % counted and its own queries still open."""
    return with_progress(filters.audits(include_cancelled))


def erp_value_of_audit():
    """Total ERP value of one audit, as a subquery (so joins to lines / queries in the same
    query can't multiply the sum)."""
    total = (
        AuditLine.objects.filter(audit=OuterRef("pk"))
        .order_by()
        .values("audit")
        .annotate(total=Sum("erp_value"))
        .values("total")
    )
    return Subquery(total, output_field=DecimalField(max_digits=20, decimal_places=2))


def with_progress(audits):
    """Adds Branch code, lines, % counted and open queries to each audit.
    Used by the dashboard status grid and the Audits list."""
    audits = audits.annotate(
        branch_code=F("branch__code"),
        line_count=Count("lines", distinct=True),
        counted_count=Count(
            "lines", filter=~Q(lines__count_status=AuditLine.Status.NOT_COUNTED), distinct=True
        ),
        open_queries=Count(
            "queries", filter=~Q(queries__status=Query.Status.CLOSED), distinct=True
        ),
    )
    # % counted as a whole number, worked out by the database so the table can sort on it.
    return audits.annotate(
        pct_counted=(F("counted_count") * 100) / F("line_count"),
    )


# ---------- Queries & actions ----------


def open_queries(filters: DashboardFilters):
    """Queries not Closed, raised by an audit inside the filters."""
    queries = Query.objects.filter(audit__in=filters.final_audits())
    if filters.items:
        queries = queries.filter(item__in=filters.item_ids)
    return queries.exclude(status=Query.Status.CLOSED)


def ageing_rows(filters: DashboardFilters, today: date | None = None) -> list[dict]:
    """Per Branch: how many open queries fall in each age group (days since raised)."""
    today = today or timezone.localdate()
    counts = {branch.pk: [0] * len(AGE_BUCKETS) for branch in filters.branches}
    for branch_id, created_at in open_queries(filters).values_list(
        "audit__branch_id", "created_at"
    ):
        age = (today - timezone.localtime(created_at).date()).days
        for index, (_label, low, high) in enumerate(AGE_BUCKETS):
            if age >= low and (high is None or age <= high):
                counts[branch_id][index] += 1
                break
    return [
        {"branch": branch.code, "buckets": counts[branch.pk], "total": sum(counts[branch.pk])}
        for branch in filters.branches
    ]


def pending_actions(filters: DashboardFilters, today: date | None = None):
    """Actions not yet Verified (Open, or Done and waiting to verify), from queries raised by
    an audit inside the filters. Each row gets branch_code, audit_title, item_code,
    owner_name, overdue ("yes" = due date passed) and days_late (negative = not due yet)."""
    today = today or timezone.localdate()
    actions = Action.objects.filter(query__audit__in=filters.final_audits())
    if filters.items:
        actions = actions.filter(query__item__in=filters.item_ids)
    return actions.exclude(status=Action.Status.VERIFIED).annotate(
        branch_code=F("query__audit__branch__code"),
        audit_title=F("query__audit__title"),
        item_code=F("query__line__item_code"),
        # Full name, or the username when the full name is blank.
        owner_name=Coalesce(NullIf(F("owner__full_name"), Value("")), F("owner__username")),
        overdue=Case(
            When(due_date__lt=today, then=Value(OVERDUE)),
            default=Value(NOT_LATE),
            output_field=CharField(),
        ),
        # Bigger number = longer late; sorting on it is the same as on the due date.
        days_late=ExpressionWrapper(
            Value(today, output_field=DateField()) - F("due_date"),
            output_field=DurationField(),
        ),
    )


def overdue_actions(filters: DashboardFilters, today: date | None = None):
    """Pending actions whose due date has passed, oldest due date first."""
    return pending_actions(filters, today).filter(overdue=OVERDUE).order_by("due_date", "id")


def _action_counts(today: date) -> dict:
    # "Overdue" counts on the due date itself: an aggregate filtered on the `overdue`
    # annotation comes back 0 on SQLite.
    return {
        "open": Count("id", filter=Q(status=Action.Status.OPEN)),
        "waiting": Count("id", filter=Q(status=Action.Status.DONE)),
        "overdue": Count("id", filter=Q(due_date__lt=today)),
        "total": Count("id"),
    }


def pending_action_rows(filters: DashboardFilters, today: date | None = None) -> list[dict]:
    """Per Branch: pending actions split into Open / Waiting to verify, and how many are late.
    Every picked Branch gets a row, also with 0."""
    today = today or timezone.localdate()
    found = {
        row["branch_code"]: row
        for row in pending_actions(filters, today)
        .order_by()
        .values("branch_code")
        .annotate(**_action_counts(today))
    }
    empty = {"open": 0, "waiting": 0, "overdue": 0, "total": 0}
    return [{"branch": branch.code, **found.get(branch.code, empty)} for branch in filters.branches]


def owner_rows(filters: DashboardFilters, today: date | None = None) -> list[dict]:
    """Per owner: pending actions, most overdue first (spots people with too much on them)."""
    today = today or timezone.localdate()
    rows = (
        pending_actions(filters, today)
        .order_by()
        .values("owner_name")
        .annotate(**_action_counts(today))
    )
    return sorted(rows, key=lambda row: (-row["overdue"], -row["total"], row["owner_name"]))


def closing_speed(filters: DashboardFilters) -> list[dict]:
    """Per Branch: queries Closed (raised by an audit inside the filters), and the average
    and slowest days from raised to closed. Branches with nothing closed are left out."""
    queries = Query.objects.filter(
        audit__in=filters.final_audits(), status=Query.Status.CLOSED, closed_at__isnull=False
    )
    if filters.items:
        queries = queries.filter(item__in=filters.item_ids)
    days: dict = defaultdict(list)
    for code, created_at, closed_at in queries.values_list(
        "audit__branch__code", "created_at", "closed_at"
    ):
        days[code].append(
            (timezone.localtime(closed_at).date() - timezone.localtime(created_at).date()).days
        )
    return [
        {
            "branch": branch.code,
            "closed": len(days[branch.code]),
            "avg_days": round(sum(days[branch.code]) / len(days[branch.code]), 1),
            "max_days": max(days[branch.code]),
        }
        for branch in filters.branches
        if days.get(branch.code)
    ]


# ---------- Variance ----------


def _excess_shortage():
    """Excess = sum of positive variance values; Shortage = sum of negative ones, as a
    positive number. All counted lines count, also small ones inside the tolerance."""
    return {
        "excess": Sum("variance_value", filter=Q(variance_value__gt=0), default=ZERO),
        "shortage": -Sum("variance_value", filter=Q(variance_value__lt=0), default=ZERO),
    }


def item_lines(filters: DashboardFilters):
    """Lines of the final audits, only the picked items (all items when none picked)."""
    lines = AuditLine.objects.filter(audit__in=filters.final_audits())
    if filters.items:
        lines = lines.filter(item__in=filters.item_ids)
    return lines


def counted_lines(filters: DashboardFilters):
    return item_lines(filters).exclude(count_status=AuditLine.Status.NOT_COUNTED)


def variance_rows(filters: DashboardFilters) -> list[dict]:
    """Excess, Shortage and Net per Branch and month (month of the audit date)."""
    rows = (
        counted_lines(filters)
        .annotate(month=TruncMonth("audit__audit_date"))
        .values("audit__branch_id", "audit__branch__code", "month")
        .annotate(**_excess_shortage())
        .order_by("month", "audit__branch__code")
    )
    return [
        {
            "month": row["month"],
            "branch_id": row["audit__branch_id"],
            "branch": row["audit__branch__code"],
            "excess": _money(row["excess"]),
            "shortage": _money(row["shortage"]),
            "net": _money(row["excess"]) - _money(row["shortage"]),
        }
        for row in rows
    ]


def variance_by_month(rows: list[dict]) -> list[dict]:
    """All picked Branches added up per month, for the chart."""
    totals: dict = {}
    for row in rows:
        total = totals.setdefault(
            row["month"], {"month": row["month"], "excess": ZERO, "shortage": ZERO}
        )
        total["excess"] += row["excess"]
        total["shortage"] += row["shortage"]
    for total in totals.values():
        total["net"] = total["excess"] - total["shortage"]
    return sorted(totals.values(), key=lambda total: total["month"])


TOP_LOSS_ITEMS = 10


def top_loss_items(filters: DashboardFilters, limit: int = TOP_LOSS_ITEMS) -> list[dict]:
    """The items with the biggest total shortage value in the picked Branches and months
    (Shortage lines only), biggest loss first."""
    rows = (
        counted_lines(filters)
        .filter(count_status=AuditLine.Status.SHORTAGE)
        .order_by()
        .values("item_id", "item__code", "item__description")
        .annotate(times=Count("id"), shortage=-Sum("variance_value"))
        .order_by("-shortage", "item__code")[:limit]
    )
    return [
        {
            "item_id": row["item_id"],
            "item_code": row["item__code"],
            "description": row["item__description"],
            "times": row["times"],
            "shortage": _money(row["shortage"]),
        }
        for row in rows
    ]


# ---------- Headline numbers (KPI tiles above the tabs) ----------


def kpis(filters: DashboardFilters, today: date | None = None) -> dict:
    """The few numbers shown as tiles on every tab, each the same as on its own tab."""
    today = today or timezone.localdate()
    audits = status_rows(filters)
    audit_totals = audits.aggregate(
        total=Count("id"), done=Count("id", filter=Q(stage__in=QUERY_STAGES))
    )
    percents = [pct for pct in audits.values_list("pct_counted", flat=True) if pct is not None]
    actions = pending_actions(filters, today).aggregate(
        total=Count("id"), overdue=Count("id", filter=Q(due_date__lt=today))
    )
    variance = counted_lines(filters).aggregate(**_excess_shortage())
    return {
        "audits": audit_totals["total"],
        "audits_done": audit_totals["done"],
        "avg_counted": round(sum(percents) / len(percents)) if percents else None,
        "open_queries": open_queries(filters).count(),
        "pending_actions": actions["total"],
        "overdue_actions": actions["overdue"],
        "net_variance": _money(variance["excess"]) - _money(variance["shortage"]),
    }


# ---------- Repeat items ----------

GRID_AUDITS = 6  # the repeat grid shows the last 6 audits of each row's Branch
STREAK_WORDS = {AuditLine.Status.SHORTAGE: "short", AuditLine.Status.EXCESS: "excess"}
CELL_STATES = {
    AuditLine.Status.SHORTAGE: "short",
    AuditLine.Status.EXCESS: "excess",
    AuditLine.Status.OK: "ok",
    AuditLine.Status.NOT_COUNTED: "not_counted",
}


def _streak_direction(statuses: set) -> str:
    """'short' / 'excess' when the whole run went one way, else 'mixed'."""
    return STREAK_WORDS[statuses.pop()] if len(statuses) == 1 else "mixed"


def _grid_cell(audit, line) -> dict:
    """One cell of the repeat grid: what the item looked like in one audit."""
    if audit is None:
        return {"state": "none"}  # the Branch has fewer than 6 audits
    cell = {"state": "missing", "audit_title": audit["title"], "audit_date": audit["audit_date"]}
    if line is None:
        return cell  # the item was not in this audit's ERP list
    value = line["variance_value"]
    cell.update(
        state=CELL_STATES.get(line["count_status"], "not_counted"),
        erp_qty=line["erp_qty"],
        physical_qty=line["physical_qty"],
        qty=line["variance_qty"],
        value=None if value is None else _money(value),
    )
    return cell


def repeat_items(filters: DashboardFilters, min_streak: int = 2) -> list[dict]:
    """Items that were Excess / Shortage in `min_streak` or more audits in a row of the same
    Branch. An audit where the item was OK, not counted or missing breaks the streak.
    Only the longest streak per item and Branch is shown (the latest one on a tie).
    Each row also gets `cells`: the item in the last 6 audits of its Branch (oldest first)."""
    audits = filters.final_audits().order_by("branch_id", "audit_date", "id")
    position = {}  # audit id -> its place in the Branch's row of audits
    branch_audits: dict = defaultdict(list)  # branch id -> its audits, oldest first
    for audit in audits.values("id", "branch_id", "title", "audit_date"):
        position[audit["id"]] = len(branch_audits[audit["branch_id"]])
        branch_audits[audit["branch_id"]].append(audit)

    flagged = defaultdict(list)  # (branch id, code, item) -> [(place, value, line id, status)]
    lines = item_lines(filters).filter(count_status__in=FLAGGED)
    for audit_id, branch_id, branch_code, item_id, value, line_id, status in lines.values_list(
        "audit_id",
        "audit__branch_id",
        "audit__branch__code",
        "item_id",
        "variance_value",
        "id",
        "count_status",
    ):
        flagged[(branch_id, branch_code, item_id)].append(
            (position[audit_id], value, line_id, status)
        )

    found = []
    for (branch_id, branch_code, item_id), hits in flagged.items():
        hits.sort()
        best: list = []
        run = [hits[0]]
        for hit in hits[1:]:
            run = run + [hit] if hit[0] == run[-1][0] + 1 else [hit]
            if len(run) >= len(best):
                best = run
        if len(run) >= len(best):
            best = run
        if len(best) >= min_streak:
            found.append((branch_id, branch_code, item_id, best))

    items = Item.objects.in_bulk({item_id for _b, _c, item_id, _r in found})
    # The last 6 audits of each Branch (padded with None on the left) and the item's line in
    # each of them, for the grid cells.
    grid_audits = {}
    for branch_id in {branch_id for branch_id, _c, _i, _r in found}:
        last = branch_audits[branch_id][-GRID_AUDITS:]
        grid_audits[branch_id] = [None] * (GRID_AUDITS - len(last)) + last
    grid_lines = {
        (line["audit_id"], line["item_id"]): line
        for line in AuditLine.objects.filter(
            audit_id__in=[audit["id"] for row in grid_audits.values() for audit in row if audit],
            item_id__in=items,
        ).values(
            "audit_id",
            "item_id",
            "count_status",
            "erp_qty",
            "physical_qty",
            "variance_qty",
            "variance_value",
        )
    }
    rows = []
    for branch_id, branch_code, item_id, run in found:
        item = items[item_id]
        rows.append(
            {
                "branch_id": branch_id,
                "branch_code": branch_code,
                "item_id": item_id,
                "item_code": item.code,
                "description": item.description,
                "uom": item.uom,
                "streak": len(run),
                "direction": _streak_direction({status for *_rest, status in run}),
                "total_value": _money(sum((value for _p, value, _l, _s in run), ZERO)),
                "cells": [
                    _grid_cell(audit, audit and grid_lines.get((audit["id"], item_id)))
                    for audit in grid_audits[branch_id]
                ],
            }
        )
    rows.sort(
        key=lambda row: (-row["streak"], row["total_value"], row["branch_code"], row["item_code"])
    )
    return rows


def grid_dates(filters: DashboardFilters) -> list | None:
    """Dates of the last 6 counted audits (oldest first, None = no audit) when exactly one
    Branch is picked, for the grid headers; None when several Branches are picked."""
    if len(filters.branches) != 1:
        return None
    newest_first = filters.final_audits().order_by("-audit_date", "-id")
    dates = list(newest_first.values_list("audit_date", flat=True)[:GRID_AUDITS])[::-1]
    return [None] * (GRID_AUDITS - len(dates)) + dates


def repeat_history(filters: DashboardFilters, branch_id: int | None, item_id: int):
    """Every line of one item in the counted audits inside the filters, newest first (OK and
    not counted too), for the repeat row and top-loss row panels. branch_id None = every
    picked Branch."""
    audits = filters.final_audits()
    if branch_id is not None:
        audits = audits.filter(branch_id=branch_id)
    return (
        AuditLine.objects.filter(audit__in=audits, item_id=item_id)
        .select_related("audit__branch", "query")
        .order_by("-audit__audit_date", "-audit_id")
    )


# ---------- Trend ----------


def trend_rows(filters: DashboardFilters, branch=None) -> list[dict]:
    """One point per audit (oldest first): excess, shortage, net and queries raised.
    With picked items, only their lines count and audits without any of them are skipped."""
    audits = filters.final_audits().select_related("branch").order_by("audit_date", "id")
    if branch is not None:
        audits = audits.filter(branch=branch)
    lines = counted_lines(filters).filter(audit__in=audits)
    sums = {
        row["audit_id"]: row
        for row in lines.values("audit_id").annotate(**_excess_shortage(), queries=Count("query"))
    }
    rows = []
    for audit in audits:
        row = sums.get(audit.pk)
        if row is None:
            if filters.items:
                continue  # none of the picked items was counted in this audit
            row = {"excess": ZERO, "shortage": ZERO, "queries": 0}
        rows.append(
            {
                "audit_id": audit.pk,
                "label": f"{audit.branch.code} {audit.title}",
                "audit_date": audit.audit_date,
                "audit_type": audit.get_audit_type_display(),
                "excess": _money(row["excess"]),
                "shortage": _money(row["shortage"]),
                "net": _money(row["excess"]) - _money(row["shortage"]),
                "queries": row["queries"],
            }
        )
    return rows


# ---------- Drilldown: the rows behind one summary row ----------


def _with_query(lines):
    """Lines with their query (if any) and its status, biggest variance value first."""
    return (
        lines.select_related("audit", "query")
        .annotate(size=Abs("variance_value"))
        .order_by(F("size").desc(nulls_last=True), "item_code")
    )


def variance_detail(filters: DashboardFilters, branch_id: int, month: date):
    """Counted lines with a variance value, in one Branch and month (the variance tab row)."""
    last_day = calendar.monthrange(month.year, month.month)[1]
    return _with_query(
        counted_lines(filters)
        .filter(
            audit__branch_id=branch_id,
            audit__audit_date__gte=month,
            audit__audit_date__lte=month.replace(day=last_day),
        )
        .exclude(variance_value=0)
    )


def audit_detail_lines(filters: DashboardFilters, audit_id: int):
    """Counted lines with a variance value in one counted audit (the trend tab row)."""
    return _with_query(counted_lines(filters).filter(audit_id=audit_id).exclude(variance_value=0))
