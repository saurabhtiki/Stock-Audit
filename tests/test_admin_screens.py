import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.urls import reverse

from accounts.models import Membership, User
from core.models import ActivityLog, Branch, Company
from tests.conftest import client_for, make_user


@pytest.mark.django_db
def test_non_admin_cannot_open_admin_screens(ravi):
    client = client_for(ravi)
    for name in [
        "core:company_list",
        "core:branch_list",
        "accounts:user_list",
        "core:activity_log",
    ]:
        assert client.get(reverse(name)).status_code == 403


@pytest.mark.django_db
def test_admin_creates_company_and_branch_with_log(admin_client):
    admin_client.post(reverse("core:company_create"), {"name": "Alpha Traders", "address": "Pune"})
    company = Company.objects.get(name="Alpha Traders")
    admin_client.post(
        reverse("core:branch_create"), {"company": company.pk, "name": "North", "code": "n1"}
    )
    branch = Branch.objects.get()
    assert branch.code == "N1"
    assert ActivityLog.objects.filter(action="create").count() == 2


@pytest.mark.django_db
def test_branch_code_unique_within_company_only(admin_client, company_a, company_b):
    Branch.objects.create(company=company_a, name="North", code="N1")
    response = admin_client.post(
        reverse("core:branch_create"), {"company": company_a.pk, "name": "X", "code": "N1"}
    )
    assert "already used" in response.content.decode()
    admin_client.post(
        reverse("core:branch_create"), {"company": company_b.pk, "name": "North", "code": "N1"}
    )
    assert Branch.objects.filter(code="N1").count() == 2


@pytest.mark.django_db
def test_logo_too_big_is_rejected(admin_client, settings):
    settings.COMPANY_LOGO_MAX_BYTES = 10
    logo = SimpleUploadedFile("logo.png", b"x" * 100, content_type="image/png")
    response = admin_client.post(
        reverse("core:company_create"), {"name": "Big Logo Co", "logo": logo}
    )
    assert not Company.objects.filter(name="Big Logo Co").exists()
    assert response.status_code == 200


@pytest.mark.django_db
def test_admin_creates_user_who_must_change_password(admin_client):
    admin_client.post(
        reverse("accounts:user_create"),
        {
            "username": "Ravi.K",
            "full_name": "Ravi K",
            "email": "Ravi@Example.com",
            "temporary_password": "Temp-pass-1",
        },
    )
    user = User.objects.get(username="ravi.k")
    assert user.email == "ravi@example.com"
    assert user.must_change_password


@pytest.mark.django_db
def test_username_and_email_unique_across_group(admin_client):
    make_user("ravi")
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "username": "RAVI",
            "full_name": "Other",
            "email": "ravi@example.com",
            "temporary_password": "Temp-pass-1",
        },
    )
    page = response.content.decode()
    assert "already taken" in page and "already used" in page
    assert User.objects.count() == 2  # admin + ravi


@pytest.mark.django_db
def test_add_membership_and_change_role(admin_client, company_a):
    user = make_user("sita")
    admin_client.post(
        reverse("accounts:membership_add", args=[user.pk]),
        {"company": company_a.pk, "role": "viewer"},
    )
    membership = Membership.objects.get(user=user)
    admin_client.post(
        reverse("accounts:membership_role", args=[membership.pk]), {"role": "manager"}
    )
    membership.refresh_from_db()
    assert membership.role == "manager"
    assert ActivityLog.objects.filter(record_type="Membership", action="update").exists()


@pytest.mark.django_db
def test_reset_password_unlocks_and_forces_change(admin_client):
    from django.utils import timezone

    user = make_user("ravi", locked_until=timezone.now() + timezone.timedelta(minutes=10))
    admin_client.post(
        reverse("accounts:user_reset_password", args=[user.pk]),
        {"temporary_password": "Fresh-pass-1"},
    )
    user.refresh_from_db()
    assert user.check_password("Fresh-pass-1")
    assert user.must_change_password and not user.is_login_locked


@pytest.mark.django_db
def test_users_are_deactivated_never_deleted(admin_client, admin_user):
    user = make_user("ravi")
    admin_client.post(reverse("accounts:user_toggle", args=[user.pk]))
    user.refresh_from_db()
    assert not user.is_active
    with pytest.raises(ValidationError):
        user.delete()
    # Admin cannot switch themself off.
    admin_client.post(reverse("accounts:user_toggle", args=[admin_user.pk]))
    admin_user.refresh_from_db()
    assert admin_user.is_active


@pytest.mark.django_db
def test_activity_log_records_old_and_new_values(admin_client, company_a):
    admin_client.post(
        reverse("core:company_edit", args=[company_a.pk]),
        {"name": "Alpha Traders Ltd", "address": ""},
    )
    entry = ActivityLog.objects.get(action="update")
    assert entry.changes["name"] == ["Alpha Traders", "Alpha Traders Ltd"]
    assert entry.actor.username == "admin"


@pytest.mark.django_db
def test_activity_log_cannot_be_changed_or_deleted(admin_user, company_a):
    from core.activity import log_activity

    entry = log_activity(admin_user, "create", company_a)
    with pytest.raises(ValidationError):
        entry.save()
    with pytest.raises(ValidationError):
        entry.delete()
    with pytest.raises(ValidationError):
        ActivityLog.objects.all().delete()
    # The database itself refuses too (triggers).
    with pytest.raises(IntegrityError), connection.cursor() as cursor:
        cursor.execute("UPDATE core_activitylog SET action = 'x'")


@pytest.mark.django_db
def test_user_search_htmx_returns_rows_only(admin_client):
    make_user("ravi")
    make_user("sita")
    response = admin_client.get(
        reverse("accounts:user_list"),
        {"q": "rav"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="user-table",
    )
    page = response.content.decode()
    assert "ravi@example.com" in page and "sita" not in page and "<html" not in page


@pytest.mark.django_db
def test_screens_render(admin_client, ravi, company_a, branches):
    membership = ravi.memberships.get(company=company_a)
    for url in [
        reverse("core:home"),
        reverse("core:company_list"),
        reverse("core:company_create"),
        reverse("core:branch_list"),
        reverse("core:branch_create"),
        reverse("accounts:user_list"),
        reverse("accounts:user_create"),
        reverse("accounts:user_detail", args=[ravi.pk]),
        reverse("accounts:rights_grid", args=[membership.pk]),
        reverse("core:activity_log"),
    ]:
        assert admin_client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_bulk_delete_is_refused_for_users_companies_branches(company_a, branches):
    make_user("ravi")
    for queryset in [User.objects.all(), Company.objects.all(), Branch.objects.all()]:
        with pytest.raises(ValidationError):
            queryset.delete()


@pytest.mark.django_db
def test_confirm_text_is_safe_for_names_with_quotes(admin_client, company_a):
    company_a.name = "Sam's Traders"
    company_a.save()
    membership = Membership.objects.create(user=make_user("sita"), company=company_a, role="user")
    Membership.objects.create(user=make_user("ravi"), company=company_a, role="user")
    page = admin_client.get(reverse("accounts:rights_grid", args=[membership.pk])).content.decode()
    assert r"Sam\u0027s Traders" in page  # escapejs output in confirm() text


@pytest.mark.django_db
def test_media_needs_login(client):
    response = client.get("/media/logos/any.png")
    assert response.status_code == 302
