"""Physical count: status math, saving a count, the offline count sheet, and Checking Done.

Example: tolerance qty 2. ERP 100, count 101 → variance +1, status OK.
ERP 100, count 96 → variance −4, status Shortage. A blank count → Not counted.
"""

import csv
import logging
import zipfile
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException

from audits.erp_import import _heading_key, _read_raw_rows, _text
from audits.models import Audit, AuditLine, CountUpload, Query, Remark
from core.activity import log_activity, log_many

logger = logging.getLogger(__name__)

Status = AuditLine.Status
FLAGGED = (Status.EXCESS, Status.SHORTAGE)
MAX_COUNT = Decimal(10) ** 11  # fits physical_qty: 14 digits, 3 decimals
REMARK_MAX = 1000


class CountError(Exception):
    """A count or action that is not allowed; the message is shown to the user."""


# ---------- Status math ----------


def work_out(erp_qty: Decimal, physical_qty, rate: Decimal, qty_tolerance, pct_tolerance):
    """(variance qty, variance value, status) for one line.

    Flagged when the difference is more than the qty tolerance OR more than the % tolerance.
    If ERP Qty is 0, any non-zero count is over the % tolerance.
    """
    if physical_qty is None:
        return None, None, Status.NOT_COUNTED
    variance = physical_qty - erp_qty
    value = (variance * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    difference = abs(variance)
    over = difference > (qty_tolerance or 0)
    if pct_tolerance is not None and difference:
        over = over or erp_qty == 0 or difference * 100 / abs(erp_qty) > pct_tolerance
    if not over:
        return variance, value, Status.OK
    return variance, value, Status.EXCESS if variance > 0 else Status.SHORTAGE


def apply_count(line: AuditLine, audit: Audit, physical_qty, actor) -> None:
    """Set the count on the line object (not saved) with variance, status, who and when."""
    line.physical_qty = physical_qty
    line.variance_qty, line.variance_value, line.count_status = work_out(
        line.erp_qty, physical_qty, line.rate, audit.qty_tolerance, audit.pct_tolerance
    )
    line.counted_by = actor if physical_qty is not None else None
    line.counted_at = timezone.now() if physical_qty is not None else None


COUNT_FIELDS = [
    "physical_qty",
    "variance_qty",
    "variance_value",
    "count_status",
    "counted_by",
    "counted_at",
]


def parse_count(raw) -> Decimal | None:
    """Text from the box or a sheet cell → a count. Blank → None. Raises CountError."""
    text = _text(raw).replace(",", "")
    if text == "":
        return None
    try:
        number = Decimal(repr(raw)) if isinstance(raw, float) else Decimal(text)
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite():
        raise CountError(f"“{_text(raw)}” is not a number. Type a count like 12 or 4.5.")
    if number < 0:
        raise CountError(f"{_text(raw)} is negative. A count must be 0 or more.")
    if -number.normalize().as_tuple().exponent > 3:
        raise CountError(f"{_text(raw)} has more than 3 decimals. Use at most 3, e.g. 2.125.")
    if number >= MAX_COUNT:
        raise CountError(f"{_text(raw)} is too big.")
    return number


def _plain(qty) -> str | None:
    return None if qty is None else format(qty.normalize(), "f")


# ---------- Saving one count from the screen ----------


@transaction.atomic
def save_count(actor, line_pk: int, raw) -> tuple[AuditLine, bool]:
    """Save one count typed in the app. Returns (line, changed). Last save wins."""
    physical_qty = parse_count(raw)
    line = AuditLine.objects.get(pk=line_pk)
    audit = Audit.objects.select_for_update().select_related("branch").get(pk=line.audit_id)
    if not audit.is_counting:
        raise CountError("Counts can only be changed while the audit is ERP Frozen.")
    line.audit = audit
    old = line.physical_qty
    if old == physical_qty:
        return line, False
    apply_count(line, audit, physical_qty, actor)
    line.save(update_fields=COUNT_FIELDS)
    log_activity(actor, "count", line, {"physical_qty": [_plain(old), _plain(physical_qty)]})
    return line, True


# ---------- Totals (counters and Checking Done summary) ----------


def count_summary(audit: Audit) -> dict:
    lines = audit.lines.all()
    summary = lines.aggregate(
        total=Count("id"),
        not_counted=Count("id", filter=Q(count_status=Status.NOT_COUNTED)),
        ok=Count("id", filter=Q(count_status=Status.OK)),
        excess=Count("id", filter=Q(count_status=Status.EXCESS)),
        shortage=Count("id", filter=Q(count_status=Status.SHORTAGE)),
        excess_value=Sum("variance_value", filter=Q(count_status=Status.EXCESS)),
        shortage_value=Sum("variance_value", filter=Q(count_status=Status.SHORTAGE)),
    )
    summary["counted"] = summary["total"] - summary["not_counted"]
    summary["queries"] = summary["excess"] + summary["shortage"]
    summary["excess_value"] = summary["excess_value"] or Decimal(0)
    summary["shortage_value"] = summary["shortage_value"] or Decimal(0)
    return summary


# ---------- Checking Done ----------


@transaction.atomic
def mark_checking_done(actor, audit_pk: int) -> int:
    """Close counting and raise one Open query per Excess / Shortage line. Returns query count."""
    audit = Audit.objects.select_for_update().select_related("branch").get(pk=audit_pk)
    if not audit.is_counting:
        raise CountError("Checking Done can only be marked while the audit is ERP Frozen.")
    summary = count_summary(audit)
    if not summary["counted"]:
        raise CountError("Nothing is counted yet. Count at least one line first.")
    flagged = audit.lines.filter(count_status__in=FLAGGED)
    Query.objects.bulk_create(
        [
            Query(audit=audit, line_id=line_id, item_id=item_id, created_by=actor)
            for line_id, item_id in flagged.values_list("pk", "item_id")
        ],
        batch_size=2000,
    )
    audit.stage = Audit.Stage.CHECKING_DONE
    audit.checking_done_by = actor
    audit.checking_done_at = timezone.now()
    audit.save(update_fields=["stage", "checking_done_by", "checking_done_at"])
    log_activity(
        actor,
        "checking done",
        audit,
        {
            "stage": [Audit.Stage.ERP_FROZEN, Audit.Stage.CHECKING_DONE],
            "counted": [None, summary["counted"]],
            "not_counted": [None, summary["not_counted"]],
            "queries": [None, summary["queries"]],
        },
    )
    return summary["queries"]


# ---------- Offline count sheet ----------

SHEET_HEADINGS = ["Item No", "Description", "UoM", "Physical Qty", "Remark"]
SHEET_KEYS = {
    _heading_key("Item No"): "code",
    _heading_key("Physical Qty"): "qty",
    _heading_key("Remark"): "remark",
}
SHEET_INFO_KEYS = {_heading_key("Description"), _heading_key("UoM")}  # read-only in the sheet
SHEET_PREVIEW_ROWS = 50


def sheet_workbook(audit: Audit) -> Workbook:
    """Blind count sheet: no ERP Qty, and Physical Qty always empty."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Count sheet"
    sheet.append(SHEET_HEADINGS)
    fill = PatternFill("solid", fgColor="1E4D8C")
    for cell, width in zip(sheet[1], (18, 45, 10, 14, 40), strict=True):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        sheet.column_dimensions[cell.column_letter].width = width
    for code, description, uom in audit.lines.order_by("item_code").values_list(
        "item_code", "description", "uom"
    ):
        sheet.append([code, description, uom, None, None])
    for row in sheet.iter_rows(min_row=2, max_col=3):
        for cell in row:
            if cell.data_type == "f":  # text like "=1+1" must stay text, not run as a formula
                cell.data_type = "s"
    sheet.freeze_panes = "A2"
    return workbook


@dataclass
class SheetRow:
    row_number: int
    code: str
    qty: Decimal | None = None
    remark: str = ""
    line_id: int | None = None
    old_qty: Decimal | None = None  # count already in the app, if the sheet changes it
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SheetCheck:
    file_errors: list[str] = field(default_factory=list)
    rows: list[SheetRow] = field(default_factory=list)

    @property
    def error_rows(self):
        return [row for row in self.rows if row.errors]

    @property
    def warning_rows(self):
        return [row for row in self.rows if row.warnings]

    @property
    def error_count(self) -> int:
        return len(self.file_errors) + len(self.error_rows)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    @property
    def count_rows(self):
        return [row for row in self.rows if row.qty is not None]

    @property
    def remark_rows(self):
        return [row for row in self.rows if row.remark]

    @property
    def preview_rows(self):
        return self.rows[:SHEET_PREVIEW_ROWS]


def _sheet_columns(headings: list, file_errors: list[str]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for position, heading in enumerate(headings):
        key = _heading_key(heading)
        if not key or key in SHEET_INFO_KEYS:
            continue
        if key not in SHEET_KEYS:
            file_errors.append(
                f"Column “{_text(heading)}” is not in the count sheet. "
                "Use the sheet downloaded from this audit."
            )
        elif SHEET_KEYS[key] in positions:
            file_errors.append(f"Column “{_text(heading)}” appears twice.")
        else:
            positions[SHEET_KEYS[key]] = position
    for heading in ("Item No", "Physical Qty"):
        if SHEET_KEYS[_heading_key(heading)] not in positions:
            file_errors.append(f"Column “{heading}” not found.")
    return positions


def check_sheet(audit: Audit, uploaded_file, name: str) -> SheetCheck:
    """Check every row against the audit's lines. Nothing is saved here."""
    result = SheetCheck()
    if not name.lower().endswith((".xlsx", ".csv")):
        result.file_errors.append("Only .xlsx (Excel) or .csv files can be uploaded.")
        return result
    limit = settings.ERP_UPLOAD_MAX_BYTES
    if uploaded_file.size > limit:
        result.file_errors.append(f"File is too big. Maximum size is {limit // (1024 * 1024)} MB.")
        return result
    try:
        raw_rows = _read_raw_rows(uploaded_file, name)
    except (InvalidFileException, zipfile.BadZipFile, OSError, ValueError, KeyError, csv.Error):
        logger.warning("Could not read count sheet %s", name, exc_info=True)
        result.file_errors.append(
            "This file could not be read. Save it again as .xlsx or .csv from the count sheet."
        )
        return result
    header_index = next((i for i, row in enumerate(raw_rows) if any(_text(c) for c in row)), None)
    if header_index is None:
        result.file_errors.append("The file is empty.")
        return result
    positions = _sheet_columns(raw_rows[header_index], result.file_errors)
    if result.file_errors:
        return result

    lines = {
        code.lower(): (pk, qty)
        for pk, code, qty in audit.lines.values_list("pk", "item_code", "physical_qty")
    }
    first_row_of_code: dict[str, int] = {}
    for row_number, raw in enumerate(raw_rows[header_index + 1 :], start=header_index + 2):
        cells = {key: (raw[pos] if pos < len(raw) else None) for key, pos in positions.items()}
        row = SheetRow(row_number=row_number, code=_text(cells["code"]))
        remark = str(cells.get("remark") or "").strip()  # keep line breaks inside a remark
        if _text(cells["qty"]) == "" and not remark:
            continue  # nothing to save on this row (blank never overwrites)
        if len(result.rows) >= settings.ERP_UPLOAD_MAX_ROWS:
            result.rows = []
            result.file_errors.append("The file has more lines than this audit can have.")
            return result
        if not row.code:
            row.errors.append("Item No is missing.")
        elif row.code.lower() not in lines:
            row.errors.append(f"Item {row.code} is not in this audit.")
        else:
            first = first_row_of_code.setdefault(row.code.lower(), row_number)
            if first != row_number:
                row.errors.append(f"Item No {row.code} is repeated (first on row {first}).")
            row.line_id, current = lines[row.code.lower()]
            try:
                row.qty = parse_count(cells["qty"])
            except CountError as error:
                row.errors.append(f"Physical Qty: {error}")
            if row.qty is not None and current is not None and current != row.qty:
                row.old_qty = current
                row.warnings.append(
                    f"Already counted {_plain(current)}; sheet says {_plain(row.qty)}."
                )
        if len(remark) > REMARK_MAX:
            row.errors.append(f"Remark is longer than {REMARK_MAX} characters.")
        row.remark = remark
        result.rows.append(row)
    if not result.rows and not result.file_errors:
        result.file_errors.append("No counts or remarks found. Fill the Physical Qty column.")
    return result


def sheet_rows_to_json(check: SheetCheck) -> list[list]:
    return [
        [row.row_number, row.code, row.line_id, _plain(row.qty), row.remark] for row in check.rows
    ]


def saved_sheet(upload: CountUpload) -> SheetCheck:
    """Rebuild the check from a pending upload, with warnings worked out from today's counts."""
    current = dict(upload.audit.lines.values_list("pk", "physical_qty"))
    rows = []
    for number, code, line_id, qty, remark in upload.rows:
        row = SheetRow(
            row_number=number,
            code=code,
            line_id=line_id,
            qty=None if qty is None else Decimal(qty),
            remark=remark,
        )
        old = current.get(line_id)
        if row.qty is not None and old is not None and old != row.qty:
            row.old_qty = old
            row.warnings.append(f"Already counted {_plain(old)}; sheet says {_plain(row.qty)}.")
        rows.append(row)
    return SheetCheck(rows=rows)


@transaction.atomic
def confirm_sheet(actor, upload: CountUpload, accept_warnings: bool, expected_changes: int) -> dict:
    """Save the sheet's counts and add its remarks. Returns what changed."""
    audit = Audit.objects.select_for_update().select_related("branch").get(pk=upload.audit_id)
    upload.refresh_from_db()
    if not audit.is_counting:
        raise CountError("Counting is closed for this audit. The sheet can no longer be saved.")
    if upload.status != CountUpload.Status.PENDING:
        raise CountError("This sheet was already saved. Upload it again if you need to.")
    check = saved_sheet(upload)
    changing = len(check.warning_rows)
    if changing > expected_changes:
        # Someone counted more lines after the preview: show the new list before overwriting.
        raise CountError(
            f"Counts changed since the preview: {changing} already-counted lines would now be "
            f"changed, not {expected_changes}. Check the list below and confirm again."
        )
    if changing and not accept_warnings:
        raise CountError(
            f"Tick the box to confirm changing {changing} count{'s' if changing != 1 else ''} "
            "already entered."
        )
    by_id = {row.line_id: row for row in check.count_rows}
    changed, logs = [], []
    for line in audit.lines.filter(pk__in=by_id):
        line.audit = audit
        new = by_id[line.pk].qty
        if line.physical_qty == new:
            continue
        old = line.physical_qty
        apply_count(line, audit, new, actor)
        changed.append(line)
        logs.append((line, {"physical_qty": [_plain(old), _plain(new)]}))
    AuditLine.objects.bulk_update(changed, COUNT_FIELDS, batch_size=2000)
    log_many(actor, "count (sheet)", logs)
    remarks = Remark.objects.bulk_create(
        [
            Remark(audit=audit, line_id=row.line_id, text=row.remark, created_by=actor)
            for row in check.remark_rows
        ],
        batch_size=2000,
    )
    upload.status = CountUpload.Status.USED
    upload.rows = []
    upload.save(update_fields=["status", "rows"])
    result = {"counts": len(changed), "remarks": len(remarks), "file": upload.original_name}
    log_activity(actor, "upload count sheet", audit, {key: [None, v] for key, v in result.items()})
    return result
