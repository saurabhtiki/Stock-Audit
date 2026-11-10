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
from django_datatable import formula_safe
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
from audits.models import (
    Action,
    Attachment,
    Audit,
    AuditLine,
    Query,
    QueryReply,
    QueryUpdate,
    Remark,
)
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


def _photo_cell(attachment, styles, width, anchor: str = "", link: str = ""):
    """One photo with its caption. `anchor` marks it as a link target, `link` jumps to its line."""
    extra = f"<br/>{link}" if link else ""
    try:
        with attachment.file.open("rb") as handle:
            data = io.BytesIO(handle.read())
        image = Image(data)
    except (OSError, ValueError) as error:
        logger.error("Photo %s missing or unreadable for PDF: %s", attachment.pk, error)
        return Paragraph(
            f"{anchor}{_esc(f'Photo missing: {attachment.original_name}')}{extra}", styles["small"]
        )
    scale = min(width / image.imageWidth, 70 * mm / image.imageHeight)
    image.drawWidth, image.drawHeight = image.imageWidth * scale, image.imageHeight * scale
    caption = (
        f"{attachment.target_label} · {_name(attachment.uploaded_by)} · "
        f"{timezone.localtime(attachment.uploaded_at):%d %b %Y}"
    )
    title = attachment.note or attachment.original_name
    text = f"{anchor}<b>{_esc(title)}</b><br/>{_esc(caption)}{extra}"
    return [image, Paragraph(text, styles["small"])]


LINK = "#1d4ed8"  # blue for links inside the PDF
REMARK_GREY = "#555555"


def _line_id_of(attachment) -> int | None:
    """Id of the audit line a photo / file is about (None for the whole audit)."""
    line = attachment.about_line
    return line.pk if line else None


def _logo(company, height):
    """The company logo scaled to `height` (at most 5 cm wide), or None if there is none."""
    if not company.logo:
        return None
    try:
        with company.logo.open("rb") as handle:
            image = Image(io.BytesIO(handle.read()))
    except (OSError, ValueError) as error:
        logger.error("Logo of company %s missing or unreadable for PDF: %s", company.pk, error)
        return None
    scale = min(height / image.imageHeight, 50 * mm / image.imageWidth)
    image.drawWidth, image.drawHeight = image.imageWidth * scale, image.imageHeight * scale
    return image


def _remark_text(remarks) -> str:
    """One line's remarks as small grey text: “Remark: Found 36 kg in rack B – Het Shah, 07 Nov 2026”."""
    return "<br/>".join(
        f'<font color="{REMARK_GREY}"><i>Remark:</i> {_esc(r.text)} – '
        f"{_esc(_name(r.created_by))}, {_when(r.created_at)}</font>"
        for r in remarks
    )


def _query_text(query, actions, latest_update) -> str:
    """A line's query as small text: status, reply (+ latest update), closing note and actions.
    “Query: Open · Reply: 2 cartons sent to Branch B – Het Shah, 07 Nov 2026”."""
    parts = [f"<b>Query: {_esc(query.get_status_display())}</b>"]
    reply = _reply_of(query)
    parts.append(
        f"<i>Reply:</i> {_esc(reply.text)} – {_esc(_name(reply.created_by))}, {_when(reply.created_at)}"
        if reply
        else "<i>No reply yet</i>"
    )
    text = " · ".join(parts)
    if latest_update:
        text += (
            f"<br/><i>Latest update:</i> {_esc(latest_update.text)} – "
            f"{_esc(_name(latest_update.created_by))}, {_when(latest_update.created_at)}"
        )
    if query.is_closed:
        note = f": {_esc(query.close_note)}" if query.close_note else ""
        text += (
            f"<br/><i>Closed</i> by {_esc(_name(query.closed_by))}, {_when(query.closed_at)}{note}"
        )
    for action in actions:
        due = ""
        if action.due_date:
            due = f", due {action.due_date:%d %b %Y}"
            if action.is_overdue:
                due += ' <font color="#b91c1c"><b>(overdue)</b></font>'
        text += (
            f"<br/><i>Action:</i> {_esc(action.description)} – {_esc(_name(action.owner))}, "
            f"{_esc(action.get_status_display())}{due}"
        )
    return f'<font color="{REMARK_GREY}">{text}</font>'


def _status_cell(line) -> str:
    """Count status for the PDF; Shortage in bold red so it stands out."""
    text = _esc(line.get_count_status_display())
    if line.count_status == AuditLine.Status.SHORTAGE:
        return f'<font color="#b91c1c"><b>{text}</b></font>'
    return text


def _lines_table(headings, lines, make_cells, widths, styles, notes_by_line, right_cols=()):
    """A table of audit lines; each line's remarks and query sit in a full-width grey row right
    under it. `make_cells(line)` and `notes_by_line` hold Paragraph markup (already escaped)."""
    small = styles["small"]
    data = [headings]
    style = TableStyle(GRID.getCommands())
    for col in right_cols:
        style.add("ALIGN", (col, 1), (col, -1), "RIGHT")
    for line in lines:
        data.append([Paragraph(cell, small) for cell in make_cells(line)])
        notes = notes_by_line.get(line.pk)
        if notes:
            data.append([Paragraph(notes, small)] + [""] * (len(headings) - 1))
            row = len(data) - 1
            style.add("SPAN", (0, row), (-1, row))
            style.add("BACKGROUND", (0, row), (-1, row), colors.HexColor("#f4f4f5"))
    table = LongTable(data, colWidths=widths, repeatRows=1)
    table.setStyle(style)
    return table


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
    title = Paragraph(_esc(f"{company.name} – Stock audit report"), styles["title"])
    logo = _logo(company, 14 * mm)
    if logo is None:
        story.append(title)
    else:
        heading = Table([[logo, title]], colWidths=[logo.drawWidth + 4 * mm, None])
        heading.setStyle(
            TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (0, 0), 0)])
        )
        story.append(heading)
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
    query_count = audit.queries.count()
    if query_count:
        closed_count = audit.queries.filter(status=Query.Status.CLOSED).count()
        summary_rows.append(
            [
                "Queries",
                f"{query_count:,}",
                "Not closed / Closed",
                f"{query_count - closed_count:,} / {closed_count:,}",
            ]
        )
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

    # Remarks of a line print right under that line; the Remarks section keeps general ones.
    remarks_by_line: dict = {}
    general_remarks = []
    for remark in _remarks(audit):
        if remark.line_id:
            remarks_by_line.setdefault(remark.line_id, []).append(remark)
        else:
            general_remarks.append(remark)

    # Photos ticked for the report, and other files (videos, documents), each tied to its line.
    attachments = (
        Attachment.objects.filter(audit=audit, is_deleted=False)
        .select_related("line", "query__line", "action__query__line", "uploaded_by")
        .order_by("uploaded_at", "id")
    )
    photos = attachments.filter(kind=Attachment.Kind.PHOTO, include_in_report=True)
    ticked_photo_count = photos.count()
    printed_photos = list(photos[: settings.PDF_MAX_PHOTOS])
    other_files = list(attachments.exclude(kind=Attachment.Kind.PHOTO))
    photos_by_line: dict = {}
    for photo in printed_photos:
        photos_by_line.setdefault(_line_id_of(photo), []).append(photo)
    files_by_line: dict = {}
    for attachment in other_files:
        files_by_line.setdefault(_line_id_of(attachment), []).append(attachment)

    variance_lines = list(
        audit.lines.filter(
            count_status__in=(AuditLine.Status.EXCESS, AuditLine.Status.SHORTAGE)
        ).order_by("item_code")
    )
    not_counted = list(
        audit.lines.filter(count_status=AuditLine.Status.NOT_COUNTED).order_by("item_code")
    )
    # Lines printed in a table above, so links can jump to them.
    printed_line_ids = {line.pk for line in variance_lines} | {line.pk for line in not_counted}

    # Each line's query (status, reply, actions) prints under it, after its remarks.
    queries = list(_queries(audit))
    actions_by_query: dict = {}
    for action in _actions(audit):
        actions_by_query.setdefault(action.query_id, []).append(action)
    latest_update_by_query = {}
    for update in (
        QueryUpdate.objects.filter(query__audit=audit)
        .select_related("created_by")
        .order_by("created_at", "id")
    ):
        latest_update_by_query[update.query_id] = update  # the last one wins
    query_text_by_line = {
        query.line_id: _query_text(
            query, actions_by_query.get(query.pk, []), latest_update_by_query.get(query.pk)
        )
        for query in queries
    }
    notes_by_line: dict = {}
    for line_id in set(remarks_by_line) | set(query_text_by_line):
        notes = []
        if line_id in remarks_by_line:
            notes.append(_remark_text(remarks_by_line[line_id]))
        if line_id in query_text_by_line:
            notes.append(query_text_by_line[line_id])
        notes_by_line[line_id] = "<br/>".join(notes)

    def line_link(line_id, text: str) -> str:
        if line_id in printed_line_ids:
            return f'<a href="#line-{line_id}" color="{LINK}">{text}</a>'
        return text

    def item_cell(line) -> str:
        """Item No with an anchor (photos, files and queries jump here) and links to its media."""
        text = f'<a name="line-{line.pk}"/>{_esc(line.item_code)}'
        photo_count = len(photos_by_line.get(line.pk, []))
        if photo_count:
            text += (
                f'<br/><a href="#photos-line-{line.pk}" color="{LINK}">Photos ({photo_count})</a>'
            )
        file_count = len(files_by_line.get(line.pk, []))
        if file_count:
            text += f'<br/><a href="#other-files" color="{LINK}">Files ({file_count})</a>'
        return text

    # Variances
    story.append(Paragraph(f"Variances ({len(variance_lines):,} lines)", styles["h2"]))
    if variance_lines:
        story.append(
            _lines_table(
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
                variance_lines,
                lambda ln: [
                    item_cell(ln),
                    _esc(ln.description),
                    _esc(ln.uom),
                    _qty(ln.erp_qty),
                    _qty(ln.physical_qty),
                    _qty(ln.variance_qty),
                    _num(ln.variance_value),
                    _status_cell(ln),
                ],
                [w * width for w in (0.12, 0.29, 0.07, 0.1, 0.1, 0.1, 0.11, 0.11)],
                styles,
                notes_by_line,
                right_cols=(3, 4, 5, 6),
            )
        )
    else:
        story.append(Paragraph("No Excess or Shortage lines.", body))

    # Not counted
    story.append(Paragraph(f"Not counted ({summary['not_counted']:,} lines)", styles["h2"]))
    if not_counted:
        story.append(
            _lines_table(
                ["Item No", "Description", "UoM", "ERP Qty", "ERP value"],
                not_counted,
                lambda ln: [
                    item_cell(ln),
                    _esc(ln.description),
                    _esc(ln.uom),
                    _qty(ln.erp_qty),
                    _num(ln.erp_value),
                ],
                [w * width for w in (0.15, 0.5, 0.1, 0.12, 0.13)],
                styles,
                notes_by_line,
                right_cols=(3, 4),
            )
        )
    else:
        story.append(Paragraph("Every line was counted.", body))

    # General remarks. Remarks on OK lines are in no table above, so they get a short list
    # of their own and nothing is left out of the report.
    story.append(Paragraph(f"General remarks ({len(general_remarks):,})", styles["h2"]))
    if general_remarks:
        story.append(
            _table(
                ["Remark", "By", "When"],
                [
                    [remark.text, _name(remark.created_by), _when(remark.created_at)]
                    for remark in general_remarks
                ],
                [w * width for w in (0.7, 0.15, 0.15)],
                styles,
            )
        )
    else:
        story.append(Paragraph("No general remarks.", body))
    ok_line_remarks = [
        remark
        for line_id, line_remarks in remarks_by_line.items()
        if line_id not in printed_line_ids
        for remark in line_remarks
    ]
    if ok_line_remarks:
        ok_line_remarks.sort(key=lambda remark: (remark.line.item_code, remark.created_at))
        story.append(Paragraph(f"Remarks on OK lines ({len(ok_line_remarks):,})", styles["h2"]))
        story.append(
            _table(
                ["Item No", "Remark", "By", "When"],
                [
                    [
                        remark.line.item_code,
                        remark.text,
                        _name(remark.created_by),
                        _when(remark.created_at),
                    ]
                    for remark in ok_line_remarks
                ],
                [w * width for w in (0.14, 0.56, 0.15, 0.15)],
                styles,
            )
        )

    # Queries are printed under their lines. A query whose line is in no table above (e.g. a
    # recount made it OK) gets a short list here, so nothing is left out of the report.
    other_queries = [query for query in queries if query.line_id not in printed_line_ids]
    if other_queries:
        story.append(Paragraph(f"Other queries ({len(other_queries):,})", styles["h2"]))
        story.append(
            _table(
                ["Item No", "Query"],
                [
                    [
                        query.line.item_code,
                        Paragraph(query_text_by_line[query.line_id], styles["small"]),
                    ]
                    for query in other_queries
                ],
                [w * width for w in (0.14, 0.86)],
                styles,
            )
        )

    # Photos: each caption links back to its line; a line's first photo is where that line's
    # "Photos (n)" link lands.
    if ticked_photo_count:
        story.append(Paragraph(f"Photos ({ticked_photo_count:,})", styles["h2"]))
        if ticked_photo_count > len(printed_photos):
            story.append(
                Paragraph(
                    f"Only the first {len(printed_photos)} of {ticked_photo_count:,} ticked photos "
                    "are printed.",
                    body,
                )
            )
        cell_width = width / 2 - 4 * mm
        anchored: set = set()
        cells = []
        for photo in printed_photos:
            line_id = _line_id_of(photo)
            anchor = ""
            if line_id and line_id not in anchored:
                anchor = f'<a name="photos-line-{line_id}"/>'
                anchored.add(line_id)
            link = line_link(line_id, "Go to the line") if line_id in printed_line_ids else ""
            cells.append(_photo_cell(photo, styles, cell_width, anchor=anchor, link=link))
        if len(cells) % 2:
            cells.append("")
        grid = Table([cells[i : i + 2] for i in range(0, len(cells), 2)], colWidths=[width / 2] * 2)
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(grid)

    # Other files (videos, documents) can't be printed: list them with where they belong.
    if other_files:
        story.append(
            Paragraph(f'<a name="other-files"/>Other files ({len(other_files):,})', styles["h2"])
        )
        story.append(
            _table(
                ["Belongs to", "Kind", "File", "Note", "By", "When"],
                [
                    [
                        Paragraph(
                            line_link(_line_id_of(attachment), _esc(attachment.target_label)),
                            styles["small"],
                        ),
                        attachment.get_kind_display(),
                        attachment.original_name,
                        attachment.note,
                        _name(attachment.uploaded_by),
                        _when(attachment.uploaded_at),
                    ]
                    for attachment in other_files
                ],
                [w * width for w in (0.16, 0.08, 0.26, 0.24, 0.13, 0.13)],
                styles,
            )
        )
        story.append(
            Paragraph(
                "Open them in the app: audit page → Photos &amp; files, or the line's remarks window.",
                styles["small"],
            )
        )

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
