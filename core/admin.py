"""Django admin panel: developer only (switched on in development settings)."""

from django.contrib import admin

from core.models import ActivityLog, Branch, Company

admin.site.register(Company)
admin.site.register(Branch)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "actor", "action", "record_type", "record_label"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
