from io import BytesIO
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from core.models import ActivityLog, Department
from tests.conftest import client_for


def make_picture(width=1200, height=800, image_format="PNG", name="me.png"):
    buffer = BytesIO()
    Image.new("RGB", (width, height), "teal").save(buffer, format=image_format)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@pytest.fixture(autouse=True)
def photo_folder(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


# ---------- department ----------


@pytest.mark.django_db
def test_admin_adds_department_to_a_company(admin_client, company_a):
    admin_client.post(
        reverse("core:department_create"), {"company": company_a.pk, "name": "  Stores "}
    )
    department = Department.objects.get(company=company_a)
    assert department.name == "Stores"
    assert ActivityLog.objects.filter(record_type="Department", action="create").exists()


@pytest.mark.django_db
def test_department_name_is_unique_within_a_company_only(admin_client, company_a, company_b):
    Department.objects.create(company=company_a, name="Stores")
    page = admin_client.post(
        reverse("core:department_create"), {"company": company_a.pk, "name": "stores"}
    )
    assert "already has a department" in page.content.decode()
    admin_client.post(
        reverse("core:department_create"), {"company": company_b.pk, "name": "Stores"}
    )
    assert Department.objects.filter(name="Stores").count() == 2


@pytest.mark.django_db
def test_admin_picks_department_for_a_user_in_a_company(admin_client, ravi, company_a):
    stores = Department.objects.create(company=company_a, name="Stores")
    membership = ravi.memberships.get(company=company_a)
    admin_client.post(
        reverse("accounts:membership_department", args=[membership.pk]), {"department": stores.pk}
    )
    membership.refresh_from_db()
    assert membership.department == stores
    entry = ActivityLog.objects.filter(record_type="Membership", action="update").latest("pk")
    assert entry.changes["department"] == [None, "Stores"]
    page = admin_client.get(reverse("accounts:user_list")).content.decode()
    assert "(Stores)" in page


@pytest.mark.django_db
def test_department_of_another_company_is_refused(admin_client, ravi, company_a, company_b):
    other = Department.objects.create(company=company_b, name="Finance")
    membership = ravi.memberships.get(company=company_a)
    admin_client.post(
        reverse("accounts:membership_department", args=[membership.pk]), {"department": other.pk}
    )
    membership.refresh_from_db()
    assert membership.department is None


@pytest.mark.django_db
def test_user_cannot_set_own_department(ravi, company_a):
    stores = Department.objects.create(company=company_a, name="Stores")
    membership = ravi.memberships.get(company=company_a)
    response = client_for(ravi).post(
        reverse("accounts:membership_department", args=[membership.pk]), {"department": stores.pk}
    )
    assert response.status_code == 403
    membership.refresh_from_db()
    assert membership.department is None


@pytest.mark.django_db
def test_inactive_department_is_not_offered_for_new_picks(admin_client, ravi, company_a):
    Department.objects.create(company=company_a, name="Old", is_active=False)
    page = admin_client.get(reverse("accounts:user_detail", args=[ravi.pk])).content.decode()
    assert "Old" not in page


# ---------- photo ----------


@pytest.mark.django_db
def test_user_adds_photo_which_is_shrunk_and_logged(ravi):
    client = client_for(ravi)
    response = client.post(reverse("accounts:profile"), {"photo": make_picture()})
    assert response.status_code == 302
    ravi.refresh_from_db()
    assert ravi.photo
    with Image.open(ravi.photo.path) as saved:
        assert max(saved.size) <= 400
    entry = ActivityLog.objects.filter(record_type="User", action="update").latest("pk")
    assert entry.changes["photo"][0] is None and entry.changes["photo"][1].startswith("users/")


@pytest.mark.django_db
def test_replacing_photo_removes_the_old_file(ravi):
    client = client_for(ravi)
    client.post(reverse("accounts:profile"), {"photo": make_picture()})
    ravi.refresh_from_db()
    first_path = ravi.photo.path
    client.post(reverse("accounts:profile"), {"photo": make_picture(name="again.png")})
    ravi.refresh_from_db()
    assert ravi.photo.path != first_path
    assert not Path(first_path).exists()


@pytest.mark.django_db
def test_photo_that_is_not_a_picture_is_refused(ravi):
    client = client_for(ravi)
    fake = SimpleUploadedFile("notes.png", b"this is text, not a picture", content_type="image/png")
    response = client.post(reverse("accounts:profile"), {"photo": fake})
    assert response.status_code == 200
    ravi.refresh_from_db()
    assert not ravi.photo


@pytest.mark.django_db
def test_photo_with_wrong_type_or_too_big_is_refused(ravi, settings):
    client = client_for(ravi)
    gif = SimpleUploadedFile("pic.gif", make_picture().read(), content_type="image/gif")
    assert (
        "png" in client.post(reverse("accounts:profile"), {"photo": gif}).content.decode().lower()
    )
    settings.USER_PHOTO_MAX_BYTES = 10
    big = client.post(reverse("accounts:profile"), {"photo": make_picture()})
    assert "too big" in big.content.decode()
    ravi.refresh_from_db()
    assert not ravi.photo


@pytest.mark.django_db
def test_saving_without_choosing_a_file_shows_a_message(ravi):
    response = client_for(ravi).post(reverse("accounts:profile"), {})
    assert "Choose a PNG or JPG" in response.content.decode()
    assert not ActivityLog.objects.filter(record_type="User").exists()


@pytest.mark.django_db
def test_user_removes_photo(ravi):
    client = client_for(ravi)
    client.post(reverse("accounts:profile"), {"photo": make_picture()})
    client.post(reverse("accounts:profile_photo_remove"))
    ravi.refresh_from_db()
    assert not ravi.photo


@pytest.mark.django_db
def test_profile_needs_login(client):
    assert client.get(reverse("accounts:profile")).status_code == 302


# ---------- theme toggle and tables ----------


@pytest.mark.django_db
def test_pages_have_theme_toggle_and_table_tools(admin_client, company_a, branches):
    for name in ["core:company_list", "core:branch_list", "accounts:user_list", "core:home"]:
        page = admin_client.get(reverse(name)).content.decode()
        assert 'id="theme-toggle"' in page and "js/theme.js" in page, name
    for name in ["core:company_list", "core:branch_list", "accounts:user_list"]:
        page = admin_client.get(reverse(name)).content.decode()
        assert "data-table=" in page and "js/table_tools.js" in page, name


@pytest.mark.django_db
def test_login_page_has_theme_toggle(client):
    assert 'id="theme-toggle"' in client.get(reverse("accounts:login")).content.decode()
