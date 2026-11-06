from django.shortcuts import redirect
from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from accounts.access import user_companies, user_role

SESSION_COMPANY_KEY = "company_id"


class ForcePasswordChangeMiddleware:
    """New users and users with a reset password must pick their own password first."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.must_change_password:
            allowed = {reverse("accounts:password_change"), reverse("accounts:logout")}
            if request.path not in allowed and not request.path.startswith("/static/"):
                return redirect("accounts:password_change")
        return self.get_response(request)


class CurrentCompanyMiddleware:
    """Sets request.company (the company the user is working in now) and request.companies."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.company = None
        request.companies = []
        request.role = ""
        if request.user.is_authenticated:
            companies = list(user_companies(request.user))
            chosen_id = request.session.get(SESSION_COMPANY_KEY)
            current = next((c for c in companies if c.pk == chosen_id), None)
            if current is None and companies:
                current = companies[0]
                request.session[SESSION_COMPANY_KEY] = current.pk
            request.companies = companies
            request.company = current
            # Looked up only when a page shows it (top bar, home), not on every table click.
            request.role = SimpleLazyObject(lambda: user_role(request.user, current))
        return self.get_response(request)
