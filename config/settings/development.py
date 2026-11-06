from .base import *
from .base import INSTALLED_APPS

DEBUG = True
WHITENOISE_AUTOREFRESH = True  # pick up CSS / JS changes without a restart

# Django's built-in admin panel is for the developer only (switched off in production).
INSTALLED_APPS = ["django.contrib.admin", *INSTALLED_APPS]
DJANGO_ADMIN_ENABLED = True
