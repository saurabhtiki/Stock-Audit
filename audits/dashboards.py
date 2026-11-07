"""Numbers behind the Dashboard tabs. Each function takes the shared filters and returns
plain rows, so the screen, the charts and the CSV all show the same figures.

Only Branches where the user has “View reports” are ever looked at (see `read_filters`).
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Count, DecimalField, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Abs, TruncMonth
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
FILTER_PARAMS = ("branch", "from", "to", "type")


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
    """The filter bar: Branches (subset of the allowed ones), month range and audit type."""

    allowed: list  # Branches the user may see on the dashboard
    branches: list
    month_from: date
    month_to: date  # first day of the last month shown
    audit_type: str = ""  # "" = all
    errors: list[str] = field(default_factory=list)

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
    return DashboardFilters(allowed, branches, month_from, month_to, audit_type, errors)


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
    return Query.objects.filter(audit__in=filters.final_audits()).exclude(
        status=Query.Status.CLOSED
    )


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


def overdue_actions(filters: DashboardFilters, today: date | None = None) -> list[dict]:
    """Actions past their due date and not Verified, oldest due date first."""
    today = today or timezone.localdate()
    actions = (
        Action.objects.filter(query__audit__in=filters.final_audits(), due_date__lt=today)
        .exclude(status=Action.Status.VERIFIED)
        .select_related("owner", "query__audit__branch", "query__line")
        .order_by("due_date", "id")
    )
    return [
        {
            "branch": action.query.audit.branch.code,
            "audit": action.query.audit.title,
            "item_code": action.query.line.item_code,
            "description": action.description,
            "owner": action.owner.full_name or action.owner.username,
            "status": action.get_status_display(),
            "due_date": action.due_date,
            "days_late": (today - action.due_date).days,
            "query_id": action.query_id,
        }
        for action in actions
    ]


# ---------- Variance ----------


def _excess_shortage():
    """Excess = sum of positive variance values; Shortage = sum of negative ones, as a
    positive number. All counted lines count, also small ones inside the tolerance."""
    return {
        "excess": Sum("variance_value", filter=Q(variance_value__gt=0), default=ZERO),
        "shortage": -Sum("variance_value", filter=Q(variance_value__lt=0), default=ZERO),
    }


def counted_lines(filters: DashboardFilters):
    return AuditLine.objects.filter(audit__in=filters.final_audits()).exclude(
        count_status=AuditLine.Status.NOT_COUNTED
    )


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


# ---------- Repeat items ----------


def repeat_items(filters: DashboardFilters, min_streak: int = 2) -> list[dict]:
    """Items that were Excess / Shortage in `min_streak` or more audits in a row of the same
    Branch. An audit where the item was OK, not counted or missing breaks the streak.
    Only the longest streak per item and Branch is shown (the latest one on a tie)."""
    audits = filters.final_audits().order_by("branch_id", "audit_date", "id")
    position = {}  # audit id -> its place in the Branch's row of audits
    counters: dict = defaultdict(int)
    for audit_id, branch_id in audits.values_list("id", "branch_id"):
        position[audit_id] = counters[branch_id]
        counters[branch_id] += 1

    flagged = defaultdict(list)  # (branch id, code, item) -> [(place, value, line id)]
    lines = AuditLine.objects.filter(audit_id__in=position, count_status__in=FLAGGED)
    for audit_id, branch_id, branch_code, item_id, value, line_id in lines.values_list(
        "audit_id", "audit__branch_id", "audit__branch__code", "item_id", "variance_value", "id"
    ):
        flagged[(branch_id, branch_code, item_id)].append((position[audit_id], value, line_id))

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
    last_queries = dict(
        Query.objects.filter(line_id__in=[run[-1][2] for _b, _c, _i, run in found]).values_list(
            "line_id", "id"
        )
    )
    rows = [
        {
            "branch_id": branch_id,
            "branch": branch_code,
            "item_id": item_id,
            "item_code": items[item_id].code,
            "description": items[item_id].description,
            "streak": len(run),
            "total_value": _money(sum((value for _p, value, _l in run), ZERO)),
            "query_id": last_queries.get(run[-1][2]),
        }
        for branch_id, branch_code, item_id, run in found
    ]
    rows.sort(key=lambda row: (-row["streak"], row["total_value"], row["branch"], row["item_code"]))
    return rows


# ---------- Trend ----------


def find_item(company, code: str):
    code = code.strip()
    if not code:
        return None
    return Item.objects.filter(company=company, code__iexact=code).first()


def trend_rows(filters: DashboardFilters, branch=None, item=None) -> list[dict]:
    """One point per audit (oldest first): excess, shortage, net and queries raised.
    For an Item: only that item's line in each audit, across the picked Branches."""
    audits = filters.final_audits().select_related("branch").order_by("audit_date", "id")
    if branch is not None:
        audits = audits.filter(branch=branch)
    lines = counted_lines(filters).filter(audit__in=audits)
    if item is not None:
        lines = lines.filter(item=item)
    sums = {
        row["audit_id"]: row
        for row in lines.values("audit_id").annotate(**_excess_shortage(), queries=Count("query"))
    }
    rows = []
    for audit in audits:
        row = sums.get(audit.pk)
        if row is None:
            if item is not None:
                continue  # the item was not counted in this audit
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


def item_runs(filters: DashboardFilters, branch_id: int, item_id: int) -> list:
    """Every counted audit of the Branch in the filters, oldest first, with this item's line
    (None when the item was not in that audit), so the run and its breaks are both visible."""
    audits = list(filters.final_audits().filter(branch_id=branch_id).order_by("audit_date", "id"))
    lines = {
        line.audit_id: line
        for line in AuditLine.objects.filter(audit__in=audits, item_id=item_id).select_related(
            "query"
        )
    }
    return [{"audit": audit, "line": lines.get(audit.pk)} for audit in audits]
