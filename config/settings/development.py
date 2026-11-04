from .base import *
from .base import INSTALLED_APPS

DEBUG = True

# Django's built-in admin panel is for the developer only (switched off in production).
INSTALLED_APPS = ["django.contrib.admin", *INSTALLED_APPS]
DJANGO_ADMIN_ENABLED = True
