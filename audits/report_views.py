"""Download one audit as Excel (5 sheets) or as a printable PDF."""

import logging

from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify

from audits import reports
from audits.models import Audit
from audits.permissions import can_download_pdf, can_view_reports, forbidden, get_visible_audit

logger = logging.getLogger(__name__)

NO_REPORT_RIGHT = "You need the “Reports” right in this Branch to download audit reports."


def _file_name(audit: Audit, extension: str) -> str:
    base = slugify(f"{audit.branch.code}-{audit.title}") or f"audit-{audit.pk}"
    return f"{base}_{timezone.localdate().isoformat()}.{extension}"


def audit_excel(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_view_reports(request.user, audit):
        return forbidden(request, NO_REPORT_RIGHT)
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{_file_name(audit, "xlsx")}"'
    reports.excel_workbook(audit).save(response)
    logger.info("%s downloaded Excel of audit %s", request.user.username, audit.pk)
    return response


def audit_pdf(request, pk):
    audit = get_visible_audit(request, pk)
    if not can_download_pdf(request.user, audit):
        return forbidden(
            request, NO_REPORT_RIGHT + " The PDF is available once ERP stock is frozen."
        )
    data = reports.audit_pdf(audit, stockkeeper=request.GET.get("stockkeeper", ""))
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{_file_name(audit, "pdf")}"'
    logger.info("%s downloaded PDF of audit %s", request.user.username, audit.pk)
    return response
