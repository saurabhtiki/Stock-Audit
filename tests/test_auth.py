from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.models import ActivityLog
from tests.conftest import PASSWORD, make_user

LOGIN = reverse("accounts:login")


def post_login(client, username, password):
    return client.post(LOGIN, {"username": username, "password": password})


@pytest.mark.django_db
def test_health_is_open_without_login(client):
    assert client.get(reverse("core:health")).json() == {"status": "ok"}


@pytest.mark.django_db
def test_pages_need_login(client):
    response = client.get(reverse("core:home"))
    assert response.status_code == 302
    assert response.url.startswith(LOGIN)


@pytest.mark.django_db
def test_login_ok_and_username_is_case_insensitive(client):
    make_user("ravi")
    response = post_login(client, "RAVI ", PASSWORD)
    assert response.status_code == 302
    assert response.url == reverse("core:home")


@pytest.mark.django_db
def test_wrong_password_shows_clear_message(client):
    make_user("ravi")
    response = post_login(client, "ravi", "wrong-password")
    assert "Username or password is wrong." in response.content.decode()


@pytest.mark.django_db
def test_unknown_user_gets_same_message(client):
    response = post_login(client, "nobody", "whatever1")
    assert "Username or password is wrong." in response.content.decode()


@pytest.mark.django_db
def test_deactivated_user_is_blocked(client):
    make_user("ravi", is_active=False)
    response = post_login(client, "ravi", PASSWORD)
    assert "switched off" in response.content.decode()
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_five_wrong_tries_lock_login_for_15_minutes(client):
    user = make_user("ravi")
    for _ in range(5):
        post_login(client, "ravi", "wrong-password")
    user.refresh_from_db()
    assert user.is_login_locked
    assert timedelta(minutes=14) < user.locked_until - timezone.now() <= timedelta(minutes=15)

    # Even the right password is refused while locked.
    response = post_login(client, "ravi", PASSWORD)
    assert "Too many wrong tries" in response.content.decode()
    assert ActivityLog.objects.filter(action="login_locked").exists()


@pytest.mark.django_db
def test_lock_expires(client):
    make_user("ravi", locked_until=timezone.now() - timedelta(minutes=1))
    assert post_login(client, "ravi", PASSWORD).status_code == 302


@pytest.mark.django_db
def test_new_user_must_change_password_first(client):
    make_user("ravi", must_change_password=True)
    post_login(client, "ravi", PASSWORD)
    response = client.get(reverse("core:home"))
    assert response.url == reverse("accounts:password_change")

    response = client.post(
        reverse("accounts:password_change"),
        {
            "old_password": PASSWORD,
            "new_password1": "Brand-new-99",
            "new_password2": "Brand-new-99",
        },
    )
    assert response.status_code == 302
    assert not User.objects.get(username="ravi").must_change_password
    assert client.get(reverse("core:home")).status_code == 200


@pytest.mark.django_db
def test_short_password_rejected(client):
    make_user("ravi", must_change_password=True)
    post_login(client, "ravi", PASSWORD)
    response = client.post(
        reverse("accounts:password_change"),
        {"old_password": PASSWORD, "new_password1": "short", "new_password2": "short"},
    )
    assert response.status_code == 200
    assert "at least 8 characters" in response.content.decode()


@pytest.mark.django_db
def test_login_ignores_outside_next_url(client):
    make_user("ravi")
    response = client.post(
        f"{LOGIN}?next=https://evil.example.com/", {"username": "ravi", "password": PASSWORD}
    )
    assert response.url == reverse("core:home")


def test_session_expires_after_8_hours_idle(settings):
    assert settings.SESSION_COOKIE_AGE == 8 * 60 * 60
    assert settings.SESSION_SAVE_EVERY_REQUEST is True
