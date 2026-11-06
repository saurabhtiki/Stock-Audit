"""The TEST environment: your dress rehearsal. Behaves like production (debug off, real login
rules) but holds fake data in data/test.db and media/test/. Start it with DJANGO_ENV=test.
The automatic tests (pytest) use config/settings/pytest.py instead and never touch these."""

from .base import *
from .base import BASE_DIR, DATABASES

# Set here too, so loading these settings directly (IDE, --settings) can never
# fall back to the development database or media folder.
DJANGO_ENV = "test"
DATABASES["default"]["NAME"] = BASE_DIR / "data" / "test.db"
MEDIA_ROOT = BASE_DIR / "media" / "test"
DEBUG = False
DJANGO_ADMIN_ENABLED = False
