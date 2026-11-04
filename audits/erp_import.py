"""Read an ERP stock file (.xlsx or .csv), check every row, and save it as the audit's ERP Draft.

Checking never saves anything. Saving happens only in `confirm_upload`.
"""

import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException

from audits.models import Audit, AuditLine, ErpUpload, Item
from core.activity import log_activity

logger = logging.getLogger(__name__)

# (heading in the template, key used in code)
COLUMNS = [
    ("Item No", "code"),
    ("Description", "description"),
    ("UoM", "uom"),
    ("ERP Qty", "qty"),
    ("Rate p.u.", "rate"),
]
ALLOWED_EXTENSIONS = (".xlsx", ".csv")
PREVIEW_ROWS = 50


def _heading_key(text) -> str:
    """'Rate p.u.' / 'RATE PU' / ' rate  p.u ' -> 'ratepu'."""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


HEADING_KEYS = {_heading_key(heading): key for heading, key in COLUMNS}


@dataclass
class ErpRow:
    row_number: int  # row number as the user sees it in Excel
    code: str
    description: str
    uom: str
    qty: Decimal | None
    rate: Decimal | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def value(self) -> Decimal | None:
        if self.qty is None or self.rate is None:
            return None
        return (self.qty * self.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ErpCheck:
    """Result of checking a file: file-level errors, rows, and totals."""

    file_errors: list[str] = field(default_factory=list)
    rows: list[ErpRow] = field(default_factory=list)

    @property
    def error_rows(self) -> list[ErpRow]:
        return [row for row in self.rows if row.errors]

    @property
    def warning_rows(self) -> list[ErpRow]:
        return [row for row in self.rows if row.warnings]

    @property
    def error_count(self) -> int:
        return len(self.file_errors) + len(self.error_rows)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    @property
    def total_value(self) -> Decimal:
        return sum((row.value for row in self.rows if row.value is not None), Decimal(0))

    @property
    def preview_rows(self) -> list[ErpRow]:
        return self.rows[:PREVIEW_ROWS]


# ---------- Template ----------


def template_workbook() -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ERP stock"
    sheet.append([heading for heading, _key in COLUMNS])
    fill = PatternFill("solid", fgColor="1E4D8C")
    for cell, width in zip(sheet[1], (18, 45, 10, 14, 14), strict=True):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        sheet.column_dimensions[cell.column_letter].width = width
    sheet.freeze_panes = "A2"
    return workbook


# ---------- Reading ----------


def _read_raw_rows(uploaded_file, name: str) -> list[list]:
    """All rows of the first sheet / the CSV, as lists of cell values."""
    if name.lower().endswith(".xlsx"):
        workbook = load_workbook(uploaded_file, read_only=True, data_only=True)
        try:
            return [list(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]
        finally:
            workbook.close()
    raw = uploaded_file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")  # CSV saved by older Excel on Windows
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [list(row) for row in csv.reader(io.StringIO(text), dialect)]


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Excel stores code 1001 as 1001.0
    return " ".join(str(value).split())


def _number(value, label: str, places: int, errors: list[str]) -> Decimal | None:
    text = _text(value).replace(",", "")
    if text == "":
        errors.append(f"{label} is missing.")
        return None
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(text)
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite():
        errors.append(f"{label} “{_text(value)}” is not a number.")
        return None
    if -number.normalize().as_tuple().exponent > places:
        errors.append(f"{label} {_text(value)} has more than {places} decimals.")
        return None
    return number


def _find_columns(headings: list, file_errors: list[str]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for position, heading in enumerate(headings):
        key = _heading_key(heading)
        if not key:
            continue
        if key not in HEADING_KEYS:
            file_errors.append(
                f"Column “{_text(heading)}” is not in the template. "
                "Use only: Item No, Description, UoM, ERP Qty, Rate p.u."
            )
        elif HEADING_KEYS[key] in positions:
            file_errors.append(f"Column “{_text(heading)}” appears twice.")
        else:
            positions[HEADING_KEYS[key]] = position
    for heading, key in COLUMNS:
        if key not in positions:
            file_errors.append(f"Column “{heading}” not found.")
    return positions


def _check_row(row_number: int, cells: dict) -> ErpRow:
    errors: list[str] = []
    code = _text(cells["code"])
    if not code:
        errors.append("Item No is missing.")
    elif len(code) > 50:
        errors.append("Item No is longer than 50 characters.")
    qty = _number(cells["qty"], "ERP Qty", 3, errors)
    rate = _number(cells["rate"], "Rate p.u.", 2, errors)
    if rate is not None and rate < 0:
        errors.append("Rate p.u. cannot be negative.")
    row = ErpRow(
        row_number=row_number,
        code=code,
        description=_text(cells["description"])[:255],
        uom=_text(cells["uom"])[:20],
        qty=qty,
        rate=rate,
        errors=errors,
    )
    if qty is not None and qty < 0:
        row.warnings.append("ERP Qty is negative.")
    return row


def check_file(uploaded_file, name: str) -> ErpCheck:
    result = ErpCheck()
    if not name.lower().endswith(ALLOWED_EXTENSIONS):
        result.file_errors.append("Only .xlsx (Excel) or .csv files can be uploaded.")
        return result
    limit = settings.ERP_UPLOAD_MAX_BYTES
    if uploaded_file.size > limit:
        result.file_errors.append(f"File is too big. Maximum size is {limit // (1024 * 1024)} MB.")
        return result
    try:
        raw_rows = _read_raw_rows(uploaded_file, name)
    except (InvalidFileException, zipfile.BadZipFile, OSError, ValueError, KeyError, csv.Error):
        logger.warning("Could not read ERP file %s", name, exc_info=True)
        result.file_errors.append(
            "This file could not be read. Save it again as .xlsx or .csv using the template."
        )
        return result

    header_index = next((i for i, row in enumerate(raw_rows) if any(_text(c) for c in row)), None)
    if header_index is None:
        result.file_errors.append("The file is empty.")
        return result
    positions = _find_columns(raw_rows[header_index], result.file_errors)
    if result.file_errors:
        return result

    first_row_of_code: dict[str, int] = {}
    max_rows = settings.ERP_UPLOAD_MAX_ROWS
    for row_number, raw in enumerate(raw_rows[header_index + 1 :], start=header_index + 2):
        cells = {key: (raw[pos] if pos < len(raw) else None) for key, pos in positions.items()}
        if not any(_text(value) for value in cells.values()):
            continue  # fully blank row
        if len(result.rows) >= max_rows:
            result.rows = []
            result.file_errors.append(
                f"The file has more than {max_rows:,} lines. Split it or contact Admin."
            )
            return result
        row = _check_row(row_number, cells)
        if row.code:
            first = first_row_of_code.setdefault(row.code.lower(), row_number)
            if first != row_number:
                row.errors.append(f"Item No {row.code} is repeated (first on row {first}).")
        result.rows.append(row)
    if not result.rows:
        result.file_errors.append("The file has headings but no item lines.")
    return result


def check_upload(upload: ErpUpload) -> ErpCheck:
    with upload.file.open("rb") as stored:
        return check_file(stored, upload.original_name)


# ---------- Saving ----------


class UploadNotAllowed(Exception):
    """The upload cannot be confirmed (wrong stage, errors, or missing tick)."""


@transaction.atomic
def confirm_upload(actor, upload: ErpUpload, accept_warnings: bool) -> ErpUpload:
    audit = (
        Audit.objects.select_for_update().select_related("branch__company").get(pk=upload.audit_id)
    )
    upload.refresh_from_db()
    if audit.stage != Audit.Stage.ERP_DRAFT:
        raise UploadNotAllowed("ERP stock is frozen. The file can no longer be changed.")
    if upload.status != ErpUpload.Status.PENDING:
        raise UploadNotAllowed("This file was already confirmed or replaced. Upload it again.")
    check = check_upload(upload)
    if check.has_errors:
        raise UploadNotAllowed("Fix the errors in the file and upload it again.")
    if check.warning_rows and not accept_warnings:
        raise UploadNotAllowed(
            f"Tick the box to confirm you checked the {len(check.warning_rows)} "
            "negative-quantity lines."
        )

    company = audit.branch.company
    items = {item.code.lower(): item for item in Item.objects.filter(company=company)}
    new_items = [
        Item(company=company, code=row.code, description=row.description, uom=row.uom, is_new=True)
        for row in check.rows
        if row.code.lower() not in items
    ]
    if new_items:
        Item.objects.bulk_create(new_items)
        items = {item.code.lower(): item for item in Item.objects.filter(company=company)}

    previous = audit.current_upload
    # Draft lines are replaced as a whole; nothing else points at them yet.
    AuditLine.objects.filter(audit=audit).delete()
    AuditLine.objects.bulk_create(
        [
            AuditLine(
                audit=audit,
                item=items[row.code.lower()],
                item_code=row.code,
                description=row.description,
                uom=row.uom,
                erp_qty=row.qty,
                rate=row.rate,
                erp_value=row.value,
            )
            for row in check.rows
        ],
        batch_size=2000,
    )
    if previous:
        previous.status = ErpUpload.Status.REPLACED
        previous.save(update_fields=["status"])
    upload.status = ErpUpload.Status.USED
    upload.line_count = len(check.rows)
    upload.total_value = check.total_value
    upload.save(update_fields=["status", "line_count", "total_value"])

    log_activity(
        actor,
        "replace ERP file" if previous else "upload ERP file",
        audit,
        {
            "file": [previous.original_name if previous else None, upload.original_name],
            "lines": [previous.line_count if previous else None, upload.line_count],
            "total_value": [
                str(previous.total_value) if previous else None,
                str(upload.total_value),
            ],
            "new_items": [None, len(new_items)],
        },
    )
    return upload
