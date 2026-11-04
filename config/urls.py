from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("", include("audits.urls")),
    # Media (logos, later photos) is served by Django in every environment.
    # Login is required for it, like every other page.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]

if getattr(settings, "DJANGO_ADMIN_ENABLED", False):
    from django.contrib import admin

    urlpatterns.append(path("django-admin/", admin.site.urls))
