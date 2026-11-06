"""Audit exports: one Excel workbook (6 sheets) and one printable A4 PDF per audit.

Both read the same data the screens show, so the numbers match.
"""

import io
import logging
from decimal import Decimal

from django.conf import settings
from django.db.models import CharField, Q, Sum
from django.db.models.functions import Cast
from django.utils import timezone
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from audits import counting
from audits.models import Action, Attachment, Audit, AuditLine, Query, QueryReply, Remark
from core.datatable import formula_safe
from core.models import ActivityLog

logger = logging.getLogger(__name__)


# ---------- shared data ----------


def _name(user) -> str:
    return (user.full_name or user.username) if user else ""


def _when(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "hour"):
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


def _remarks(audit: Audit):
    return (
        Remark.objects.filter(audit=audit, is_deleted=False)
        .select_related("line", "created_by")
        .order_by("line__item_code", "created_at")
    )


def _queries(audit: Audit):
    return (
        Query.objects.filter(audit=audit)
        .select_related("line", "closed_by", "reply__created_by")
        .order_by("line__item_code")
    )


def _actions(audit: Audit):
    return (
        Action.objects.filter(query__audit=audit)
        .select_related("query__line", "owner", "done_by", "verified_by")
        .order_by("query__line__item_code", "created_at")
    )


def _reply_of(query: Query):
    try:
        return query.reply
    except QueryReply.DoesNotExist:
        return None


def activity_for(audit: Audit):
    """Log rows of the audit and everything in it (lines, remarks, queries, actions, media)."""

    def ids(queryset):
        return queryset.annotate(text_id=Cast("pk", CharField())).values("text_id")

    record_sets = {
        Audit: Audit.objects.filter(pk=audit.pk),
        AuditLine: AuditLine.objects.filter(audit=audit),
        Remark: Remark.objects.filter(audit=audit),
        Query: Query.objects.filter(audit=audit),
        Action: Action.objects.filter(query__audit=audit),
        Attachment: Attachment.objects.filter(audit=audit),
    }
    match = Q()
    for model, queryset in record_sets.items():
        match |= Q(record_type=model._meta.verbose_name.title(), record_id__in=ids(queryset))
    return ActivityLog.objects.filter(match).select_related("actor").order_by("created_at", "id")


# ---------- Excel ----------


def _cell(value):
    """Numbers stay numbers; text that looks like a formula is made harmless."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        return formula_safe(value)
    return value


def _sheet(workbook, title: str, headings: list[str], rows):
    sheet = workbook.create_sheet(title)
    sheet.append(headings)
    for row in rows:
        sheet.append([_cell(value) for value in row])
    return sheet


def excel_workbook(audit: Audit) -> Workbook:
    workbook = Workbook(write_only=True)
    status_labels = dict(AuditLine.Status.choices)
    lines = audit.lines.select_related("counted_by").order_by("pk")
    _sheet(
        workbook,
        "Lines",
        [
            "Item No",
            "Description",
            "UoM",
            "ERP Qty",
            "Rate p.u.",
            "ERP value",
            "Physical Qty",
            "Variance",
            "Var. value",
            "Status",
            "Counted by",
            "Counted at",
        ],
        (
            [
                line.item_code,
                line.description,
                line.uom,
                line.erp_qty,
                line.rate,
                line.erp_value,
                line.physical_qty,
                line.variance_qty,
                line.variance_value,
                status_labels[line.count_status],
                _name(line.counted_by),
                _when(line.counted_at),
            ]
            for line in lines.iterator(chunk_size=2000)
        ),
    )
    _sheet(
        workbook,
        "Remarks",
        ["Item No", "Remark", "By", "At", "Edited at"],
        (
            [
                r.line.item_code if r.line_id else "General",
                r.text,
                _name(r.created_by),
                _when(r.created_at),
                _when(r.edited_at),
            ]
            for r in _remarks(audit)
        ),
    )
    query_labels = dict(Query.Status.choices)
    query_rows = []
    for query in _queries(audit):
        reply = _reply_of(query)
        query_rows.append(
            [
                query.line.item_code,
                query.line.description,
                query.line.variance_qty,
                query.line.variance_value,
                query_labels[query.status],
                reply.text if reply else "",
                _name(reply.created_by) if reply else "",
                _name(query.closed_by),
                _when(query.closed_at),
                query.close_note,
            ]
        )
    _sheet(
        workbook,
        "Queries",
        [
            "Item No",
            "Description",
            "Variance",
            "Var. value",
            "Status",
            "Reply",
            "Reply by",
            "Closed by",
            "Closed at",
            "Close note",
        ],
        query_rows,
    )
    action_labels = dict(Action.Status.choices)
    _sheet(
        workbook,
        "Actions",
        [
            "Item No",
            "Action",
            "Owner",
            "Due date",
            "Status",
            "Done by",
            "Done at",
            "Verified by",
            "Verified at",
        ],
        (
            [
                a.query.line.item_code,
                a.description,
                _name(a.owner),
                _when(a.due_date),
                action_labels[a.status],
                _name(a.done_by),
                _when(a.done_at),
                _name(a.verified_by),
                _when(a.verified_at),
            ]
            for a in _actions(audit)
        ),
    )
    kind_labels = dict(Attachment.Kind.choices)
    _sheet(
        workbook,
        "Photos and files",
        ["Item No", "Type", "Note", "File name", "In PDF", "Added by", "Added at"],
        (
            [
                m.target_label,
                kind_labels[m.kind],
                m.note,
                m.original_name,
                "Yes" if m.include_in_report else "",
                _name(m.uploaded_by),
                _when(m.uploaded_at),
            ]
            for m in Attachment.objects.filter(audit=audit, is_deleted=False)
            .select_related("line", "query__line", "action__query__line", "uploaded_by")
            .order_by("uploaded_at", "id")
        ),
    )
    _sheet(
        workbook,
        "Activity log",
        ["When", "User", "Action", "Record", "Record id", "Label", "Changes"],
        (
            [
                _when(e.created_at),
                _name(e.actor),
                e.action,
                e.record_type,
                e.record_id,
                e.record_label,
                _changes_text(e.changes),
            ]
            for e in activity_for(audit).iterator(chunk_size=2000)
        ),
    )
    return workbook


def _changes_text(changes: dict) -> str:
    return "; ".join(f"{field}: {old} → {new}" for field, (old, new) in changes.items())


# ---------- PDF ----------


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": base["Title"],
        "h2": ParagraphStyle("h2", parent=base["Heading2"], spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontSize=9, leading=11),
        "small": ParagraphStyle("small", parent=base["BodyText"], fontSize=7.5, leading=9),
    }


def _esc(text) -> str:
    """Make text safe inside a reportlab Paragraph (it reads a little HTML)."""
    return (
        str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ).replace("\n", "<br/>")


def _num(value, places: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:,.{places}f}"


def _qty(value) -> str:
    """12.500 → 12.5, 40.000 → 40"""
    if value is None:
        return ""
    return f"{value:,.3f}".rstrip("0").rstrip(".")


GRID = TableStyle(
    [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 7.5),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe7fb")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
)


def _table(headings, rows, widths, styles, right_cols=()):
    small = styles["small"]
    data = [headings] + [
        [Paragraph(_esc(cell), small) if isinstance(cell, str) else cell for cell in row]
        for row in rows
    ]
    table = LongTable(data, colWidths=widths, repeatRows=1)
    style = TableStyle(GRID.getCommands())
    for col in right_cols:
        style.add("ALIGN", (col, 1), (col, -1), "RIGHT")
    table.setStyle(style)
    return table


def _photo_cell(attachment, styles, width):
    try:
        with attachment.file.open("rb") as handle:
            data = io.BytesIO(handle.read())
        image = Image(data)
    except (OSError, ValueError) as error:
        logger.error("Photo %s missing or unreadable for PDF: %s", attachment.pk, error)
        return Paragraph(_esc(f"Photo missing: {attachment.original_name}"), styles["small"])
    scale = min(width / image.imageWidth, 70 * mm / image.imageHeight)
    image.drawWidth, image.drawHeight = image.imageWidth * scale, image.imageHeight * scale
    caption = (
        f"{attachment.target_label} · {_name(attachment.uploaded_by)} · "
        f"{timezone.localtime(attachment.uploaded_at):%d %b %Y}"
    )
    title = attachment.note or attachment.original_name
    return [image, Paragraph(f"<b>{_esc(title)}</b><br/>{_esc(caption)}", styles["small"])]


def audit_pdf(audit: Audit, stockkeeper: str = "") -> bytes:
    styles = _styles()
    body = styles["body"]
    buffer = io.BytesIO()
    is_draft = not audit.is_locked
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=16 * mm,
        bottomMargin=14 * mm,
        title=f"{audit} – stock audit",
        author="Stock Audit",
        pageCompression=settings.PDF_COMPRESS,
    )
    width = doc.width
    printed = timezone.localtime()

    def page_frame(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(12 * mm, 8 * mm, f"{audit} · printed {printed:%d %b %Y %H:%M}")
        canvas.drawRightString(A4[0] - 12 * mm, 8 * mm, f"Page {document.page}")
        if is_draft:
            canvas.setFont("Helvetica-Bold", 10)
            canvas.setFillColor(colors.HexColor("#b45309"))
            canvas.drawCentredString(A4[0] / 2, A4[1] - 10 * mm, "DRAFT – not locked")
        canvas.restoreState()

    story = []
    company = audit.branch.company
    story.append(Paragraph(_esc(f"{company.name} – Stock audit report"), styles["title"]))
    facts = [
        ["Audit", audit.title, "Branch", str(audit.branch)],
        ["Type", audit.get_audit_type_display(), "Stage", audit.get_stage_display()],
        ["Audit date", _when(audit.audit_date), "ERP stock as on", _when(audit.stock_as_on)],
        [
            "Qty tolerance",
            _qty(audit.qty_tolerance),
            "% tolerance",
            _num(audit.pct_tolerance) if audit.pct_tolerance is not None else "not set",
        ],
        [
            "Checking Done",
            f"{_name(audit.checking_done_by)} {_when(audit.checking_done_at)}",
            "Locked",
            f"{_name(audit.locked_by)} {_when(audit.locked_at)}",
        ],
    ]
    fact_table = Table(facts, colWidths=[width * 0.16, width * 0.34] * 2)
    fact_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
                ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 8.5),
                ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 8.5),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ]
        )
    )
    story.append(fact_table)

    # Count summary
    summary = counting.count_summary(audit)
    total_value = audit.lines.aggregate(value=Sum("erp_value"))["value"] or Decimal(0)
    net = summary["excess_value"] + summary["shortage_value"]
    story.append(Paragraph("Count summary", styles["h2"]))
    summary_rows = [
        ["Lines", f"{summary['total']:,}", "ERP value", _num(total_value)],
        ["Counted", f"{summary['counted']:,}", "Not counted", f"{summary['not_counted']:,}"],
        ["OK (within tolerance)", f"{summary['ok']:,}", "", ""],
        ["Excess lines", f"{summary['excess']:,}", "Excess value", _num(summary["excess_value"])],
        [
            "Shortage lines",
            f"{summary['shortage']:,}",
            "Shortage value",
            _num(summary["shortage_value"]),
        ],
        ["", "", "Net variance", _num(net)],
    ]
    summary_table = Table(summary_rows, colWidths=[width * 0.25] * 4)
    summary_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ]
        )
    )
    story.append(summary_table)

    # Variances
    variance_lines = audit.lines.filter(
        count_status__in=(AuditLine.Status.EXCESS, AuditLine.Status.SHORTAGE)
    ).order_by("item_code")
    story.append(Paragraph(f"Variances ({len(variance_lines):,} lines)", styles["h2"]))
    if variance_lines:
        story.append(
            _table(
                [
                    "Item No",
                    "Description",
                    "UoM",
                    "ERP Qty",
                    "Physical",
                    "Variance",
                    "Var. value",
                    "Status",
                ],
                [
                    [
                        ln.item_code,
                        ln.description,
                        ln.uom,
                        _qty(ln.erp_qty),
                        _qty(ln.physical_qty),
                        _qty(ln.variance_qty),
                        _num(ln.variance_value),
                        ln.get_count_status_display(),
                    ]
                    for ln in variance_lines
                ],
                [w * width for w in (0.12, 0.32, 0.07, 0.1, 0.1, 0.1, 0.11, 0.08)],
                styles,
                right_cols=(3, 4, 5, 6),
            )
        )
    else:
        story.append(Paragraph("No Excess or Shortage lines.", body))

    # Not counted
    not_counted = audit.lines.filter(count_status=AuditLine.Status.NOT_COUNTED).order_by(
        "item_code"
    )
    story.append(Paragraph(f"Not counted ({summary['not_counted']:,} lines)", styles["h2"]))
    if summary["not_counted"]:
        story.append(
            _table(
                ["Item No", "Description", "UoM", "ERP Qty", "ERP value"],
                [
                    [ln.item_code, ln.description, ln.uom, _qty(ln.erp_qty), _num(ln.erp_value)]
                    for ln in not_counted
                ],
                [w * width for w in (0.15, 0.5, 0.1, 0.12, 0.13)],
                styles,
                right_cols=(3, 4),
            )
        )
    else:
        story.append(Paragraph("Every line was counted.", body))

    # Remarks
    remarks = list(_remarks(audit))
    story.append(Paragraph(f"Remarks ({len(remarks):,})", styles["h2"]))
    if remarks:
        story.append(
            _table(
                ["Item No", "Remark", "By", "When"],
                [
                    [
                        r.line.item_code if r.line_id else "General",
                        r.text,
                        _name(r.created_by),
                        _when(r.created_at),
                    ]
                    for r in remarks
                ],
                [w * width for w in (0.14, 0.56, 0.15, 0.15)],
                styles,
            )
        )
    else:
        story.append(Paragraph("No remarks.", body))

    # Queries and actions
    queries = list(_queries(audit))
    actions_by_query = {}
    for action in _actions(audit):
        actions_by_query.setdefault(action.query_id, []).append(action)
    story.append(Paragraph(f"Queries and actions ({len(queries):,} queries)", styles["h2"]))
    if queries:
        rows = []
        for query in queries:
            reply = _reply_of(query)
            action_text = "\n".join(
                f"• {a.description} — {_name(a.owner)}, {a.get_status_display()}"
                + (f", due {a.due_date:%d %b %Y}" if a.due_date else "")
                for a in actions_by_query.get(query.pk, [])
            )
            rows.append(
                [
                    query.line.item_code,
                    _num(query.line.variance_value),
                    query.get_status_display(),
                    reply.text if reply else "",
                    action_text or "—",
                ]
            )
        story.append(
            _table(
                ["Item No", "Var. value", "Status", "Reply", "Actions"],
                rows,
                [w * width for w in (0.12, 0.1, 0.12, 0.33, 0.33)],
                styles,
                right_cols=(1,),
            )
        )
    else:
        story.append(Paragraph("No queries.", body))

    # Photos
    photos = list(
        Attachment.objects.filter(
            audit=audit, kind=Attachment.Kind.PHOTO, include_in_report=True, is_deleted=False
        )
        .select_related("line", "query__line", "action__query__line", "uploaded_by")
        .order_by("uploaded_at", "id")
    )
    if photos:
        limit = settings.PDF_MAX_PHOTOS
        story.append(Paragraph(f"Photos ({len(photos):,})", styles["h2"]))
        if len(photos) > limit:
            story.append(
                Paragraph(
                    f"Only the first {limit} of {len(photos):,} ticked photos are printed.", body
                )
            )
        cell_width = width / 2 - 4 * mm
        cells = [_photo_cell(photo, styles, cell_width) for photo in photos[:limit]]
        if len(cells) % 2:
            cells.append("")
        grid = Table([cells[i : i + 2] for i in range(0, len(cells), 2)], colWidths=[width / 2] * 2)
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(grid)

    # Signatures
    signers = [
        ("Auditor", _name(audit.checking_done_by)),
        ("Stockkeeper", stockkeeper.strip()[:80]),
        ("Manager", _name(audit.locked_by)),
    ]
    sign_table = Table(
        [
            ["\n\n\n_______________________"] * 3,
            [Paragraph(f"<b>{_esc(role)}</b>", body) for role, _ in signers],
            [Paragraph(_esc(name or "Name: ____________________"), body) for _, name in signers],
            ["Date: ____________"] * 3,
        ],
        colWidths=[width / 3] * 3,
    )
    sign_table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9)]))
    story.append(Spacer(1, 10 * mm))
    story.append(KeepTogether([Paragraph("Signatures", styles["h2"]), sign_table]))

    doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
    return buffer.getvalue()
