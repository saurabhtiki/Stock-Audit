"""Login, logout and password change."""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.db import transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.forms import LoginForm, OwnPasswordChangeForm
from accounts.models import User
from core.activity import log_activity

logger = logging.getLogger(__name__)

WRONG_LOGIN = "Username or password is wrong."


def _minutes_left(user: User) -> int:
    seconds = (user.locked_until - timezone.now()).total_seconds()
    return max(1, int(seconds // 60) + 1)


def _record_failed_try(user: User) -> None:
    # Count in the database (F + 1) so tries sent at the same moment are all counted.
    with transaction.atomic():
        User.objects.filter(pk=user.pk).update(failed_login_count=F("failed_login_count") + 1)
        user.refresh_from_db(fields=["failed_login_count"])
        if user.failed_login_count < settings.LOGIN_MAX_FAILED_ATTEMPTS:
            return
        user.locked_until = timezone.now() + timedelta(minutes=settings.LOGIN_LOCK_MINUTES)
        user.failed_login_count = 0
        user.save(update_fields=["failed_login_count", "locked_until"])
    logger.warning("Login locked for %s after too many wrong tries", user.username)
    log_activity(None, "login_locked", user, {"locked_until": [None, str(user.locked_until)]})


@login_not_required
def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:home")

    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = form.cleaned_data["username"]
        password = form.cleaned_data["password"]
        user = User.objects.filter(username=username).first()

        if user and user.is_login_locked:
            logger.info("Login refused for %s: locked", username)
            form.add_error(
                None, f"Too many wrong tries. Try again in {_minutes_left(user)} minutes."
            )
        elif user and not user.is_active and user.check_password(password):
            logger.info("Login refused for %s: account deactivated", username)
            form.add_error(None, "Your account is switched off. Please contact Admin.")
        else:
            authenticated = authenticate(request, username=username, password=password)
            if authenticated is None:
                if user:
                    _record_failed_try(user)
                logger.info("Failed login for username %r", username)
                form.add_error(None, WRONG_LOGIN)
            else:
                authenticated.failed_login_count = 0
                authenticated.locked_until = None
                authenticated.save(update_fields=["failed_login_count", "locked_until"])
                login(request, authenticated)
                next_url = request.GET.get("next", "")
                if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
                    next_url = settings.LOGIN_REDIRECT_URL
                return redirect(next_url)

    return render(request, "accounts/login.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "You are logged out.")
    return redirect("accounts:login")


def password_change_view(request):
    form = OwnPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        update_session_auth_hash(request, user)
        log_activity(user, "password_changed", user)
        messages.success(request, "Password changed.")
        return redirect("core:home")
    return render(
        request,
        "accounts/password_change.html",
        {"form": form, "forced": request.user.must_change_password},
    )
