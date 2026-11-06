from .base import *
from .base import BASE_DIR

DJANGO_ENV = "test"  # also when the DJANGO_ENV variable is not set (pytest)
DEBUG = False
DJANGO_ADMIN_ENABLED = False

# Fast hashing keeps the test run quick. Never use this outside tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MEDIA_ROOT = BASE_DIR / "media" / "test"

# Uncompressed PDFs so tests can read their text.
PDF_COMPRESS = 0
