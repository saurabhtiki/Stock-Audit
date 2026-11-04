import logging
from functools import wraps

from django.shortcuts import render

logger = logging.getLogger(__name__)


def admin_required(view):
    """Only group Admins may open this page."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_admin:
            logger.warning("Non-admin %s tried to open %s", request.user.username, request.path)
            return render(
                request,
                "core/forbidden.html",
                {"reason": "Only Admin can open this page."},
                status=403,
            )
        return view(request, *args, **kwargs)

    return wrapper
