from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("", include("audits.urls")),
    # Only company logos and profile photos are served straight from /media/ (login required,
    # like every page). Audit attachments are served by audits.media_views after a Branch check.
    re_path(
        r"^media/(?P<path>(?:logos|users)/[^/]+)$", serve, {"document_root": settings.MEDIA_ROOT}
    ),
]

if getattr(settings, "DJANGO_ADMIN_ENABLED", False):
    from django.contrib import admin

    urlpatterns.append(path("django-admin/", admin.site.urls))
