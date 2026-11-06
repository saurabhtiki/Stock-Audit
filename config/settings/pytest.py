"""Settings for the automatic tests only (uv run pytest). Not an environment you start.

pytest-django gives every run a throw-away in-memory database, and files go to a temporary
media folder that is deleted when the run ends, so data/test.db and media/test/ stay untouched.
"""

import atexit
import shutil
import tempfile
from pathlib import Path

from .test import *

# Fast hashing keeps the test run quick. Never use this outside tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="stock-audit-pytest-media-"))
atexit.register(shutil.rmtree, MEDIA_ROOT, ignore_errors=True)

# Uncompressed PDFs so tests can read their text.
PDF_COMPRESS = 0

# Tests never load CSS / JS; WhiteNoise would re-scan static/ for every test client (slow).
MIDDLEWARE = [m for m in MIDDLEWARE if m != "whitenoise.middleware.WhiteNoiseMiddleware"]
