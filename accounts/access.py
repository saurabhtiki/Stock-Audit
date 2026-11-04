"""Who can see which company and Branch."""

from functools import reduce
from operator import or_

from django.db.models import Q, QuerySet

from accounts.models import BranchRight, Membership
from accounts.rights import RIGHT_NAMES
from core.models import Branch, Company

_ANY_RIGHT = reduce(or_, (Q(**{name: True}) for name in RIGHT_NAMES))


def user_companies(user) -> QuerySet[Company]:
    """Active companies the user can switch to (Admin: all active companies)."""
    companies = Company.objects.filter(is_active=True)
    if user.is_admin:
        return companies
    return companies.filter(memberships__user=user, memberships__is_active=True).distinct()


def user_role(user, company: Company | None) -> str:
    if user.is_admin:
        return "Admin"
    if company is None:
        return ""
    membership = Membership.objects.filter(user=user, company=company, is_active=True).first()
    return membership.get_role_display() if membership else ""


def visible_branches(user, company: Company | None) -> QuerySet[Branch]:
    """Active Branches of the company where the user has at least one right."""
    if company is None or not company.is_active:
        return Branch.objects.none()
    branches = company.branches.filter(is_active=True)
    if user.is_admin:
        return branches
    if not user_companies(user).filter(pk=company.pk).exists():
        return Branch.objects.none()
    rights = BranchRight.objects.filter(_ANY_RIGHT, user=user)
    return branches.filter(pk__in=rights.values("branch_id"))


def has_right(user, branch: Branch, right: str) -> bool:
    if right not in RIGHT_NAMES:
        raise ValueError(f"Unknown right: {right}")
    if not branch.is_usable:
        return False
    if user.is_admin:
        return True
    if not Membership.objects.filter(user=user, company=branch.company, is_active=True).exists():
        return False
    return BranchRight.objects.filter(user=user, branch=branch, **{right: True}).exists()
