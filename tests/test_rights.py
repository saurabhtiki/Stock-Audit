import pytest
from django.urls import reverse

from accounts.access import has_right, user_companies, visible_branches
from accounts.models import BranchRight, Membership
from accounts.rights import Role
from accounts.rights_service import copy_rights, rights_grid, save_rights
from core.models import ActivityLog, Branch
from tests.conftest import client_for, make_user


@pytest.mark.django_db
def test_user_has_one_role_per_company(ravi, company_a, company_b):
    client = client_for(ravi)
    page = client.get(reverse("core:home")).content.decode()
    assert "Alpha Traders" in page and "Auditor" in page

    client.post(reverse("core:switch_company"), {"company_id": company_b.pk})
    page = client.get(reverse("core:home")).content.decode()
    assert "as <strong>Manager</strong>" in page


@pytest.mark.django_db
def test_cannot_switch_to_company_without_access(company_a, company_b):
    user = make_user("sita")
    Membership.objects.create(user=user, company=company_a, role=Role.USER)
    client = client_for(user)
    client.post(reverse("core:switch_company"), {"company_id": company_b.pk})
    assert client.session["company_id"] == company_a.pk


@pytest.mark.django_db
def test_switcher_lists_only_own_companies(ravi, company_a, company_b, db):
    from core.models import Company

    Company.objects.create(name="Gamma Other")
    assert set(user_companies(ravi)) == {company_a, company_b}


@pytest.mark.django_db
def test_rights_in_one_branch_only(ravi, admin_user, company_a, branches):
    north, south = branches
    save_rights(admin_user, ravi, company_a, {north.pk: {"count_and_remarks"}})
    assert list(visible_branches(ravi, company_a)) == [north]
    assert has_right(ravi, north, "count_and_remarks")
    assert not has_right(ravi, north, "upload_erp")
    assert not has_right(ravi, south, "count_and_remarks")


@pytest.mark.django_db
def test_same_user_different_rights_in_two_branches(ravi, admin_user, company_a, branches):
    """Acceptance criterion 1."""
    north, south = branches
    save_rights(
        admin_user,
        ravi,
        company_a,
        {north.pk: {"count_and_remarks"}, south.pk: {"reply_queries", "view_reports"}},
    )
    assert has_right(ravi, north, "count_and_remarks") and not has_right(
        ravi, north, "reply_queries"
    )
    assert has_right(ravi, south, "reply_queries") and not has_right(
        ravi, south, "count_and_remarks"
    )


@pytest.mark.django_db
def test_grid_starts_with_role_defaults(ravi, company_a, branches):
    membership = ravi.memberships.get(company=company_a)
    rows = rights_grid(ravi, membership)
    assert all(not row["is_saved"] for row in rows)
    assert rows[0]["ticked"] == {"count_and_remarks", "upload_media", "view_reports"}


@pytest.mark.django_db
def test_grid_save_through_screen(admin_client, ravi, company_a, branches):
    north, south = branches
    membership = ravi.memberships.get(company=company_a)
    admin_client.post(
        reverse("accounts:rights_grid", args=[membership.pk]),
        {f"r_{north.pk}_upload_erp": "on", f"r_{south.pk}_upload_erp": "on", "r_x_bad": "on"},
    )
    assert has_right(ravi, north, "upload_erp") and has_right(ravi, south, "upload_erp")
    assert ActivityLog.objects.filter(action="rights").count() == 2


@pytest.mark.django_db
def test_copy_rights_only_touches_chosen_company(admin_user, ravi, company_a, company_b, branches):
    north, south = branches
    b_branch = Branch.objects.create(company=company_b, name="Main", code="M1")
    sita = make_user("sita")
    Membership.objects.create(user=sita, company=company_a, role=Role.USER)
    Membership.objects.create(user=sita, company=company_b, role=Role.USER)
    save_rights(admin_user, ravi, company_a, {north.pk: {"lock_audit"}})
    save_rights(admin_user, ravi, company_b, {b_branch.pk: {"view_reports"}})
    save_rights(admin_user, sita, company_b, {b_branch.pk: {"upload_erp"}})

    copy_rights(admin_user, ravi, sita, company_a)

    assert has_right(sita, north, "lock_audit")
    assert not has_right(sita, south, "lock_audit")
    # Company B untouched.
    assert has_right(sita, b_branch, "upload_erp") and not has_right(sita, b_branch, "view_reports")


@pytest.mark.django_db
def test_copy_rights_screen_rejects_user_from_other_company(
    admin_client, ravi, company_a, company_b, branches
):
    outsider = make_user("outsider")
    Membership.objects.create(user=outsider, company=company_b, role=Role.USER)
    membership = Membership.objects.create(
        user=make_user("sita"), company=company_a, role=Role.USER
    )
    response = admin_client.post(
        reverse("accounts:rights_copy", args=[membership.pk]),
        {"source_user": outsider.pk},
        follow=True,
    )
    assert "Pick another user from this company" in response.content.decode()


@pytest.mark.django_db
def test_deactivating_company_hides_branches_and_reactivating_restores(
    admin_client, admin_user, ravi, company_a, branches
):
    north, _south = branches
    save_rights(admin_user, ravi, company_a, {north.pk: {"view_reports"}})

    admin_client.post(reverse("core:company_toggle", args=[company_a.pk]))
    company_a.refresh_from_db()
    north.refresh_from_db()
    assert company_a not in user_companies(ravi)
    assert not has_right(ravi, north, "view_reports")
    assert north.is_active  # Branch's own flag kept, so reactivation restores it

    admin_client.post(reverse("core:company_toggle", args=[company_a.pk]))
    north.refresh_from_db()
    assert has_right(ravi, north, "view_reports")


@pytest.mark.django_db
def test_removed_company_access_blocks_rights(admin_user, ravi, company_a, branches):
    north, _ = branches
    save_rights(admin_user, ravi, company_a, {north.pk: {"view_reports"}})
    ravi.memberships.filter(company=company_a).update(is_active=False)
    assert not has_right(ravi, north, "view_reports")
    assert not visible_branches(ravi, company_a).exists()


@pytest.mark.django_db
def test_admin_sees_every_branch(admin_user, company_a, branches):
    assert set(visible_branches(admin_user, company_a)) == set(branches)


@pytest.mark.django_db
def test_rights_rows_are_explicit_after_save(admin_user, ravi, company_a, branches):
    save_rights(admin_user, ravi, company_a, {})
    assert BranchRight.objects.filter(user=ravi).count() == 2
    rows = rights_grid(ravi, ravi.memberships.get(company=company_a))
    assert all(row["is_saved"] and not row["ticked"] for row in rows)
