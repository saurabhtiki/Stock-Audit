"""Settings shared by all environments. Pick one with DJANGO_ENV = development | test | production."""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DJANGO_ENV = os.environ.get("DJANGO_ENV", "development")

# Each environment may have its own env file, e.g. .env.production (never committed).
load_dotenv(BASE_DIR / f".env.{DJANGO_ENV}")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key")
DEBUG = False
ALLOWED_HOSTS = [h for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
    "accounts",
    "audits",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves CSS / JS in every environment, also with debug off (test, production).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "accounts.middleware.ForcePasswordChangeMiddleware",
    "accounts.middleware.CurrentCompanyMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "accounts.context_processors.company_context",
                "core.context_processors.app_context",
                "core.context_processors.backup_reminder",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

(BASE_DIR / "data").mkdir(exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "data" / f"{DJANGO_ENV}.db",
        "OPTIONS": {
            # WAL lets many users read while one writes.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"

PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
]

# Logged out after 8 hours without activity (timer restarts on every request).
SESSION_COOKIE_AGE = int(timedelta(hours=8).total_seconds())
SESSION_SAVE_EVERY_REQUEST = True

# 5 wrong passwords in a row lock the login for 15 minutes.
LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 15

LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)  # WhiteNoise warns when it is missing
# Development and test read CSS / JS straight from static/ (no collectstatic needed).
# Production serves the collected, compressed copies (see production.py).
WHITENOISE_USE_FINDERS = True

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media" / DJANGO_ENV

COMPANY_LOGO_MAX_BYTES = 1 * 1024 * 1024
USER_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # shrunk to a small JPEG when saved
USER_PHOTO_MAX_PIXELS = 400

# ERP stock files (.xlsx or .csv).
ERP_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
ERP_UPLOAD_MAX_ROWS = 20_000

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# Attachments (Phase 5). Photos are shrunk to a JPEG; videos and files are kept as uploaded.
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_MAX_PIXELS = 1600
VIDEO_MAX_BYTES = 100 * 1024 * 1024
FILE_MAX_BYTES = 5 * 1024 * 1024
PHOTO_MAX_MEGAPIXELS = 50  # a 48 MP phone photo fits; bigger is refused before decoding
ATTACHMENT_MAX_FILES = 20  # per upload (Django's own limit of 100 stays as a hard stop)
PDF_MAX_PHOTOS = 100
PDF_COMPRESS = 1  # 0 in tests, so the PDF text can be checked
