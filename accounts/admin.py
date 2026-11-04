from django.contrib import admin

from accounts.models import BranchRight, Membership, User

admin.site.register(User)
admin.site.register(Membership)
admin.site.register(BranchRight)
