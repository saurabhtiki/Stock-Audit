import pytest
from django.test import Client

from accounts.models import Membership, User
from accounts.rights import Role
from core.models import Branch, Company

PASSWORD = "Secret-pass-123"


def make_user(username, **extra):
    extra.setdefault("must_change_password", False)
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=PASSWORD,
        full_name=username.title(),
        **extra,
    )


@pytest.fixture
def admin_user(db):
    return make_user("admin", is_admin=True)


@pytest.fixture
def company_a(db):
    return Company.objects.create(name="Alpha Traders")


@pytest.fixture
def company_b(db):
    return Company.objects.create(name="Beta Stores")


@pytest.fixture
def branches(company_a):
    north = Branch.objects.create(company=company_a, name="North", code="N1")
    south = Branch.objects.create(company=company_a, name="South", code="S1")
    return north, south


@pytest.fixture
def ravi(db, company_a, company_b):
    user = make_user("ravi")
    Membership.objects.create(user=user, company=company_a, role=Role.AUDITOR)
    Membership.objects.create(user=user, company=company_b, role=Role.MANAGER)
    return user


def client_for(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def admin_client(admin_user):
    return client_for(admin_user)
