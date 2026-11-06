import os

from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False
DJANGO_ADMIN_ENABLED = False

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if len(SECRET_KEY) < 32:
    raise ImproperlyConfigured(
        "SECRET_KEY is missing or too short. Put a long random value in .env.production."
    )

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# The spec needs HTTPS. Set SECURE_SSL_REDIRECT=false only if a proxy already forces HTTPS.
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "true").lower() == "true"
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "3600"))
# Left off on purpose: "include subdomains" and "preload" would force HTTPS on every site of
# the company's domain, not just this app. Turn them on only if IT agrees.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

# CSS / JS: the copies made by `manage.py collectstatic`, compressed, with cache-safe names.
WHITENOISE_USE_FINDERS = False
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
