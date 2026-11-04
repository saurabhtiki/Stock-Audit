"""The 9 rights and the starting ticks for each role."""

from django.db import models


class Role(models.TextChoices):
    MANAGER = "manager", "Manager"
    USER = "user", "User"
    AUDITOR = "auditor", "Auditor"
    VIEWER = "viewer", "Viewer"


# (field name, short label, tooltip)
RIGHTS = [
    ("upload_erp", "Upload ERP", "Create audits and upload ERP stock files"),
    ("set_tolerance_freeze", "Tolerance & freeze", "Set tolerance and freeze ERP stock"),
    ("count_and_remarks", "Count", "Enter physical counts and remarks; mark Checking Done"),
    ("upload_media", "Media", "Upload photos, videos and files"),
    ("reply_queries", "Reply", "Reply to queries"),
    ("manage_actions", "Actions", "Create and assign actions; mark actions done"),
    ("verify_close_queries", "Verify & close", "Verify actions and close queries"),
    ("lock_audit", "Lock", "Lock audits"),
    ("view_reports", "Reports", "View reports and dashboards"),
]
RIGHT_NAMES = [name for name, _label, _tip in RIGHTS]

ROLE_DEFAULT_RIGHTS = {
    Role.MANAGER: {
        "set_tolerance_freeze",
        "manage_actions",
        "verify_close_queries",
        "lock_audit",
        "view_reports",
    },
    Role.USER: {"upload_erp", "reply_queries", "manage_actions", "view_reports"},
    Role.AUDITOR: {"count_and_remarks", "upload_media", "view_reports"},
    Role.VIEWER: {"view_reports"},
}
