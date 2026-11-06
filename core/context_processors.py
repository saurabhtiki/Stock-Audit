from django.conf import settings


def app_context(request):
    return {"app_name": "Stock Audit", "app_env": settings.DJANGO_ENV}


def backup_reminder(request):
    """Admins only: banner data when no backup was made in the last 7 days (or ever)."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_admin):
        return {}
    from core.backup import reminder  # imports models; keep it out of startup

    return {"backup_reminder": reminder()}
