from .base import *
from .base import BASE_DIR

DEBUG = False
DJANGO_ADMIN_ENABLED = False

# Fast hashing keeps the test run quick. Never use this outside tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MEDIA_ROOT = BASE_DIR / "media" / "test"
