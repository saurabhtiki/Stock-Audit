from django.conf import settings


def app_context(request):
    return {"app_name": "Stock Audit", "app_env": settings.DJANGO_ENV}
