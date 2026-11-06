"""Phase 7: lists on the shared table, no internet files, environments, serve command."""

import re
from datetime import date
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from accounts.models import Membership
from accounts.rights import Role
from audits.models import Audit, Query
from core.activity import log_activity
from core.models import ActivityLog, Branch, Company, Department

from .conftest import client_for, make_user
from .test_audits import grant
from .test_dashboards_backup import make_audit

pytestmark = pytest.mark.django_db

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"


def page(client, name, **params):
    response = client.get(reverse(name), params)
    assert response.status_code == 200, response.content[:300]
    return response.content.decode()


def table_part(client, name, table_id, **params):
    """What htmx gets when a filter / sort / page link inside the table is clicked."""
    response = client.get(reverse(name), params, HTTP_HX_REQUEST="true", HTTP_HX_TARGET=table_id)
    body = response.content.decode()
    assert "<html" not in body and f'id="{table_id}"' in body
    return body


def csv_lines(client, name, **params):
    response = client.get(reverse(name), {**params, "export": "csv"})
    assert response["Content-Type"].startswith("text/csv")
    return b"".join(response.streaming_content).decode("utf-8-sig").splitlines()


# ---------- Companies / Branches / Departments ----------


def test_company_list_filters_and_csv(admin_client, company_a, company_b, branches):
    company_b.is_active = False
    company_b.save()
    body = table_part(admin_client, "core:company_list", "company-table", is_active="False")
    assert "Beta Stores" in body and "Alpha Traders" not in body
    lines = csv_lines(admin_client, "core:company_list", sort="name")
    assert lines[0] == "Company,Address,Branches,Status"
    assert lines[1] == "Alpha Traders,,2,Active" and lines[2] == "Beta Stores,,0,Inactive"


def test_branch_list_company_filter_and_add_prefill(admin_client, company_a, company_b, branches):
    Branch.objects.create(company=company_b, name="Main", code="M1")
    body = page(admin_client, "core:branch_list", company_name="Beta Stores")
    assert "M1" in body and "N1" not in body
    assert f"?company={company_b.pk}" in body  # "Add Branch" pre-fills the picked company
    lines = csv_lines(admin_client, "core:branch_list", q="nor")
    assert lines[1:] == ["N1,North,Alpha Traders,Active"]


def test_branch_save_returns_to_its_company_list(admin_client, company_a, branches):
    north = branches[0]
    response = admin_client.post(
        reverse("core:branch_edit", args=[north.pk]),
        {"company": company_a.pk, "name": "North Yard", "code": "N1", "is_active": "on"},
    )
    assert response.url == reverse("core:branch_list") + "?company_name=Alpha+Traders"


def test_department_list(admin_client, company_a, company_b):
    Department.objects.create(company=company_a, name="Stores")
    Department.objects.create(company=company_b, name="Finance")
    body = table_part(
        admin_client, "core:department_list", "department-table", company_name="Alpha Traders"
    )
    assert "Stores" in body and "Finance" not in body


@pytest.mark.parametrize(
    ("make", "url_name"),
    [
        (lambda company: company, "core:company_toggle"),
        (
            lambda company: Branch.objects.create(company=company, code="X1", name="X"),
            "core:branch_toggle",
        ),
        (
            lambda company: Department.objects.create(company=company, name="Stores"),
            "core:department_toggle",
        ),
    ],
)
def test_toggle_helper_for_all_three(admin_client, company_a, ravi, make, url_name):
    record = make(company_a)
    response = admin_client.post(reverse(url_name, args=[record.pk]))
    assert response.status_code == 200 and "<tr id=" in response.content.decode()
    record.refresh_from_db()
    assert record.is_active is False
    entry = ActivityLog.objects.latest("pk")
    assert entry.action == "deactivate" and entry.changes == {"is_active": [True, False]}
    # Admin only
    assert client_for(ravi).post(reverse(url_name, args=[record.pk])).status_code == 403
    record.refresh_from_db()
    assert record.is_active is False


# ---------- Users ----------


def test_user_list_shortcuts_and_columns(admin_client, company_a, company_b):
    stores = Department.objects.create(company=company_a, name="Stores")
    sita = make_user("sita")
    Membership.objects.create(user=sita, company=company_a, role=Role.AUDITOR, department=stores)
    Membership.objects.create(user=sita, company=company_b, role=Role.VIEWER)
    gone = make_user("gone", is_active=False)
    inactive = table_part(admin_client, "accounts:user_list", "user-table", is_active="False")
    assert gone.email in inactive and sita.email not in inactive
    by_department = table_part(admin_client, "accounts:user_list", "user-table", departments="stor")
    assert sita.email in by_department and gone.email not in by_department
    by_role = table_part(admin_client, "accounts:user_list", "user-table", companies="auditor")
    assert sita.email in by_role
    lines = csv_lines(admin_client, "accounts:user_list", q="sita")
    assert lines[1] == (
        "sita,Sita,sita@example.com,Stores,"
        "Alpha Traders: Auditor (Stores); Beta Stores: Viewer,No,Active"
    )


def test_user_list_admin_only(ravi):
    assert client_for(ravi).get(reverse("accounts:user_list")).status_code == 403


# ---------- Activity log ----------


def test_activity_log_50_per_page_newest_first(admin_client, admin_user, company_a):
    for number in range(60):
        log_activity(admin_user, "update", company_a, {"name": [f"old{number}", f"new{number}"]})
    body = page(admin_client, "core:activity_log")
    assert "Showing 1–50 of 60" in body
    assert body.index("new59") < body.index("new58")  # newest first
    assert "new9<" not in body  # the oldest are on page 2


def test_activity_log_filters_and_full_csv(admin_client, admin_user, ravi, company_a):
    for number in range(60):
        log_activity(admin_user, "update", company_a, {"name": [f"a{number}", f"b{number}"]})
    log_activity(ravi, "create", company_a, {"address": [None, "Pune"]})
    body = table_part(admin_client, "core:activity_log", "activity-table", actor_name="ravi")
    assert "Pune" in body and "b59" not in body
    lines = csv_lines(admin_client, "core:activity_log", actor_name="admin")
    assert len(lines) == 1 + 60  # every matching row, not just one page
    assert lines[0] == "When,Who,Did,Record type,Record,Old → new"
    assert lines[1].endswith("name: a59 → b59")
    search = csv_lines(admin_client, "core:activity_log", q="Pune")
    assert len(search) == 2 and search[1].endswith("address: — → Pune")


def test_activity_log_admin_only(ravi):
    assert client_for(ravi).get(reverse("core:activity_log")).status_code == 403


# ---------- Audits ----------


@pytest.fixture
def audits(admin_user, branches):
    north, south = branches
    counted = make_audit(
        north,
        "Aug26",
        date(2026, 8, 30),
        admin_user,
        lines=[("A1", 10, 20, 2), ("A2", 5, 5, 4), ("A3", 3, None, 1), ("A4", 8, 1, 1)],
    )
    make_audit(south, "Sep26", date(2026, 9, 1), admin_user, stage=Audit.Stage.CANCELLED)
    return counted


def test_audit_list_numbers(admin_client, audits):
    lines = csv_lines(admin_client, "audits:audit_list")
    assert lines[0] == ("Branch,Audit,Audit date,Type,Stage,Lines,ERP value,% counted,Open queries")
    # 4 lines, ERP value 20 + 20 + 3 + 8 = 51, 3 of 4 counted = 75 %, 2 queries (A1, A4)
    assert lines[1:] == ["N1,Aug26,2026-08-30,Scheduled,Checking Done,4,51,75,2"]
    Query.objects.filter(audit=audits).update(status=Query.Status.CLOSED)
    assert csv_lines(admin_client, "audits:audit_list")[1].endswith(",75,0")


def test_audit_list_cancelled_hidden_unless_asked(admin_client, audits):
    assert "Sep26" not in page(admin_client, "audits:audit_list")
    body = page(admin_client, "audits:audit_list", cancelled="1")
    assert "Sep26" in body
    # the table's own links keep "show cancelled"
    assert "cancelled=1" in table_part(
        admin_client, "audits:audit_list", "audit-table", cancelled="1", sort="title"
    )


def test_audit_list_filters_and_branch_rights(audits, branches, company_a):
    south = branches[1]
    viewer = make_user("vera")
    grant(viewer, south, company_a, role=Role.VIEWER, view_reports=True)
    client = client_for(viewer)
    assert "Aug26" not in page(client, "audits:audit_list")  # North is not hers
    assert csv_lines(client, "audits:audit_list", cancelled="1")[1:] == [
        "S1,Sep26,2026-09-01,Scheduled,Cancelled,0,,,0"
    ]
    admin_lines = csv_lines(
        client_for(make_user("root", is_admin=True)),
        "audits:audit_list",
        stage="checking_done",
        total_value_min="50",
    )
    assert len(admin_lines) == 2


def test_tick_list_counts_are_per_audit_not_per_line(admin_client, audits):
    body = page(admin_client, "audits:audit_list", cancelled="1")
    stage_counts = re.findall(
        r'data-label="(Checking Done|Cancelled)".*?\((\d+)\)', body, re.DOTALL
    )
    assert dict(stage_counts) == {"Checking Done": "1", "Cancelled": "1"}


# ---------- No internet files ----------

EXTERNAL = re.compile(
    r"""<(?:script|link)\b[^>]*\b(?:src|href)\s*=\s*["'](?:https?:)?//""", re.IGNORECASE
)


def test_no_template_loads_files_from_the_internet():
    offenders = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if EXTERNAL.search(text) or re.search(r"@import\s+url\(\s*['\"]?https?:", text):
            offenders.append(str(path.relative_to(TEMPLATES_DIR)))
    assert offenders == [], f"These templates load files from the internet: {offenders}"


def test_pages_use_local_copies(admin_client, company_a, branches):
    body = page(admin_client, "core:home")
    for local in ("css/tailwind.css", "vendor/htmx-2.0.6.min.js", "vendor/alpine-3.14.9.min.js"):
        assert f"/static/{local}" in body
    static = Path(settings.BASE_DIR) / "static"
    for file in (
        "css/tailwind.css",
        "vendor/htmx-2.0.6.min.js",
        "vendor/alpine-3.14.9.min.js",
        "vendor/chart-4.4.1.umd.min.js",
    ):
        assert (static / file).stat().st_size > 10_000, file


def test_built_css_has_daisyui_and_used_classes():
    css = (Path(settings.BASE_DIR) / "static/css/tailwind.css").read_text(encoding="utf-8")
    for needed in (".btn", ".badge-soft", ".table-zebra", ".toggle-sm", ".max-w-6xl"):
        assert needed in css, f"{needed} missing: rebuild with tools/build_css.ps1"


# ---------- Environments ----------


def test_seed_demo_runs_in_test_not_production():
    call_command("seed_demo")  # pytest runs as the test environment
    assert Company.objects.filter(name="Alpha Traders").exists()
    with override_settings(DJANGO_ENV="production"), pytest.raises(CommandError, match="never"):
        call_command("seed_demo")


def test_pytest_never_writes_into_the_real_test_media_folder():
    real_test_media = Path(settings.BASE_DIR) / "media" / "test"
    assert Path(settings.MEDIA_ROOT).resolve() != real_test_media.resolve()
    assert "stock-audit-pytest-media-" in str(settings.MEDIA_ROOT)


def test_whitenoise_is_on_for_every_real_environment():
    from config.settings import base

    assert "whitenoise.middleware.WhiteNoiseMiddleware" in base.MIDDLEWARE


# ---------- serve command ----------


def test_serve_starts_waitress_with_proxy_headers(monkeypatch):
    calls = {}
    monkeypatch.setattr("waitress.serve", lambda app, **kw: calls.update(app=app, **kw))
    call_command("serve", "--port", "8123", "--threads", "4")
    assert calls["port"] == 8123 and calls["threads"] == 4 and calls["host"] == "127.0.0.1"
    assert calls["trusted_proxy"] == "127.0.0.1"
    assert "x-forwarded-proto" in calls["trusted_proxy_headers"]
    assert callable(calls["app"])


def test_serve_without_proxy(monkeypatch):
    calls = {}
    monkeypatch.setattr("waitress.serve", lambda app, **kw: calls.update(kw))
    call_command("serve", "--trusted-proxy", "", "--host", "0.0.0.0")
    assert calls["trusted_proxy"] is None and calls["trusted_proxy_headers"] is None


def test_serve_on_production_needs_collectstatic(monkeypatch, tmp_path):
    monkeypatch.setattr("waitress.serve", lambda app, **kw: None)
    with (
        override_settings(DJANGO_ENV="production", STATIC_ROOT=tmp_path),
        pytest.raises(CommandError, match="collectstatic"),
    ):
        call_command("serve")
    (tmp_path / "staticfiles.json").write_text("{}")
    with override_settings(DJANGO_ENV="production", STATIC_ROOT=tmp_path):
        call_command("serve")


def test_add_branch_button_follows_htmx_company_filter(admin_client, company_a, company_b):
    body = table_part(admin_client, "core:branch_list", "branch-table", company_name="Beta Stores")
    assert 'id="branch-add"' in body and 'hx-swap-oob="true"' in body
    assert f"?company={company_b.pk}" in body
