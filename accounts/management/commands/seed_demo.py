"""Demo data for DEVELOPMENT and TEST only: uv run python manage.py seed_demo

Creates 2 companies, 3 Branches, an Admin and a multi-company user.
All demo logins use the password below (never on production).
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Membership, User
from accounts.rights import Role
from accounts.rights_service import save_rights
from core.models import Branch, Company

logger = logging.getLogger(__name__)

DEMO_PASSWORD = "demo-pass-123"
DEMO_USERS = [("admin", "Asha Admin", True), ("ravi", "Ravi Kumar", False)]


class Command(BaseCommand):
    help = "Fill the development or test database with demo companies, Branches and users."

    def handle(self, *args, **options):
        if settings.DJANGO_ENV not in ("development", "test"):
            logger.error("seed_demo refused in the %s environment", settings.DJANGO_ENV)
            raise CommandError(
                "seed_demo runs only in development or test, never on production "
                "(set DJANGO_ENV=development or DJANGO_ENV=test)."
            )

        alpha, _ = Company.objects.get_or_create(name="Alpha Traders", defaults={"address": "Pune"})
        beta, _ = Company.objects.get_or_create(name="Beta Stores", defaults={"address": "Nashik"})
        north, _ = Branch.objects.get_or_create(
            company=alpha, code="N1", defaults={"name": "North"}
        )
        Branch.objects.get_or_create(company=alpha, code="S1", defaults={"name": "South"})
        main, _ = Branch.objects.get_or_create(company=beta, code="M1", defaults={"name": "Main"})

        users = {}
        for username, full_name, is_admin in DEMO_USERS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "full_name": full_name,
                    "email": f"{username}@demo.local",
                    "is_admin": is_admin,
                    "must_change_password": False,
                },
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
            users[username] = user

        admin, ravi = users["admin"], users["ravi"]
        Membership.objects.get_or_create(user=ravi, company=alpha, defaults={"role": Role.AUDITOR})
        Membership.objects.get_or_create(user=ravi, company=beta, defaults={"role": Role.MANAGER})
        save_rights(admin, ravi, alpha, {north.pk: {"count_and_remarks", "upload_media"}})
        save_rights(admin, ravi, beta, {main.pk: {"lock_audit", "view_reports"}})

        self.stdout.write(self.style.SUCCESS("Demo data ready. Logins: admin, ravi."))
