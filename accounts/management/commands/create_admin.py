"""Create the first group Admin: uv run python manage.py create_admin"""

import getpass

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from core.activity import log_activity


class Command(BaseCommand):
    help = "Create a group Admin user (asks for username, name, email and password)."

    def add_arguments(self, parser):
        parser.add_argument("--username")
        parser.add_argument("--full-name")
        parser.add_argument("--email")
        parser.add_argument(
            "--password", help="Avoid on shared machines; you will be asked if left out."
        )

    def handle(self, *args, **options):
        username = (options["username"] or input("Username: ")).strip().lower()
        full_name = options["full_name"] or input("Full name: ")
        email = (options["email"] or input("Email: ")).strip().lower()
        password = options["password"] or getpass.getpass("Password (min 8 chars): ")

        if User.objects.filter(username=username).exists():
            raise CommandError(f"Username '{username}' is already taken.")
        if User.objects.filter(email=email).exists():
            raise CommandError(f"Email '{email}' is already used.")
        try:
            password_validation.validate_password(password)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            full_name=full_name,
            is_admin=True,
            is_staff=True,
            is_superuser=True,
            must_change_password=False,
        )
        log_activity(None, "create", user, {"is_admin": [None, True]})
        self.stdout.write(self.style.SUCCESS(f"Admin '{username}' created. You can log in now."))
