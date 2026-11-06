"""Phase 5: lock / reopen, media (photos, videos, files), Excel and PDF reports."""

import io
import zipfile
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import Resolver404, resolve, reverse
from openpyxl import load_workbook
from PIL import Image

from accounts.rights import Role
from audits.models import Action, Attachment, Audit, Query, Remark
from core.models import ActivityLog

from .conftest import client_for, make_user
from .test_audits import audit, grant, manager, priya  # noqa: F401
from .test_counting import anil, count, frozen  # noqa: F401
from .test_queries import boss, raised, replier  # noqa: F401

QS = Query.Status


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def locker(branches, company_a):
    """Manager with the Lock and Reports rights in North."""
    user = make_user("lata")
    grant(user, branches[0], company_a, role=Role.MANAGER, lock_audit=True, view_reports=True)
    return user


@pytest.fixture
def viewer(branches, company_a):
    user = make_user("vera")
    grant(user, branches[0], company_a, role=Role.VIEWER, view_reports=True)
    return user


@pytest.fixture
def media_user(branches, company_a):
    user = make_user("meena")
    grant(user, branches[0], company_a, role=Role.AUDITOR, upload_media=True)
    return user


def lock(user, audit_obj):
    return client_for(user).post(
        reverse("audits:audit_lock", args=[audit_obj.pk]), {"confirm": "on"}
    )


def logs(action):
    return ActivityLog.objects.filter(action=action)


def jpeg(name="shelf.jpg", size=(3000, 2000), fmt="JPEG"):
    out = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(out, fmt)
    return SimpleUploadedFile(name, out.getvalue(), content_type="image/jpeg")


def mp4(name="walk.mp4"):
    return SimpleUploadedFile(name, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)


def pdf_file(name="slip.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4\n%fake but starts right\n")


def xlsx_file(name="stock.xlsx"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return SimpleUploadedFile(name, out.getvalue())


def add_media(user, target, pk, *files, note=""):
    return client_for(user).post(
        reverse("audits:media_panel", args=[target, pk]),
        {"files": list(files), "note": note},
        HTTP_HX_REQUEST="true",
    )


def refused(response) -> bool:
    """Media refusals are shown inside the panel (htmx doesn't swap a 403 page)."""
    html = response.content.decode()
    return (
        response.status_code == 200
        and "alert-error" in html
        and ("right in this Branch" in html or "Only the person who added it" in html)
    )


def visible_attachments():
    return Attachment.objects.filter(is_deleted=False)


# ---------- Lock ----------


def test_lock_from_checking_done(raised, locker):  # noqa: F811
    response = lock(locker, raised.audit)
    assert response.status_code == 302
    audit_obj = Audit.objects.get(pk=raised.audit_id)
    assert audit_obj.stage == Audit.Stage.LOCKED
    assert audit_obj.locked_by == locker
    assert logs("lock").count() == 1


def test_lock_needs_confirm_tick(raised, locker):  # noqa: F811
    client = client_for(locker)
    response = client.post(reverse("audits:audit_lock", args=[raised.audit_id]), {})
    assert response.status_code == 200
    assert "Tick the box" in response.content.decode()
    assert Audit.objects.get(pk=raised.audit_id).stage == Audit.Stage.CHECKING_DONE


def test_lock_screen_shows_open_queries(raised, locker):  # noqa: F811
    response = client_for(locker).get(reverse("audits:audit_lock", args=[raised.audit_id]))
    assert "These carry forward" in response.content.decode()
    assert response.context["summary"]["query_open"] == 1


def test_lock_refused_before_checking_done(frozen, locker):  # noqa: F811
    assert lock(locker, frozen).status_code == 403
    frozen.refresh_from_db()
    assert frozen.stage == Audit.Stage.ERP_FROZEN


def test_lock_needs_lock_right(raised, boss):  # noqa: F811
    assert lock(boss, raised.audit).status_code == 403


def test_lock_right_in_other_branch_not_enough(raised, branches, company_a):  # noqa: F811
    other = make_user("otto")
    grant(other, branches[1], company_a, role=Role.MANAGER, lock_audit=True)
    grant(other, branches[0], company_a, role=Role.MANAGER, view_reports=True)
    assert lock(other, raised.audit).status_code == 403


# ---------- Reopen ----------


def test_reopen_admin_only_with_reason(raised, locker, admin_client):  # noqa: F811
    lock(locker, raised.audit)
    url = reverse("audits:audit_reopen", args=[raised.audit_id])
    assert client_for(locker).post(url, {"reason": "Sheet wrong"}).status_code == 403
    response = admin_client.post(url, {"reason": ""})
    assert "A reason is required" in response.content.decode()
    assert Audit.objects.get(pk=raised.audit_id).stage == Audit.Stage.LOCKED

    admin_client.post(url, {"reason": "Count sheet was wrong for 3 lines"})
    audit_obj = Audit.objects.get(pk=raised.audit_id)
    assert audit_obj.stage == Audit.Stage.CHECKING_DONE
    assert audit_obj.locked_by is None
    entry = logs("reopen").get()
    assert entry.changes["reason"] == [None, "Count sheet was wrong for 3 lines"]


def test_reopened_audit_keeps_counts_fixed(raised, locker, admin_client, anil):  # noqa: F811
    lock(locker, raised.audit)
    admin_client.post(
        reverse("audits:audit_reopen", args=[raised.audit_id]), {"reason": "Recheck please"}
    )
    count(client_for(anil), raised.audit, "A100", "95")
    assert raised.audit.lines.get(item_code="A100").physical_qty == 90


def test_lock_and_reopen_can_repeat(raised, locker, admin_client):  # noqa: F811
    url = reverse("audits:audit_reopen", args=[raised.audit_id])
    for _ in range(2):
        lock(locker, raised.audit)
        admin_client.post(url, {"reason": "Another look"})
    assert logs("lock").count() == 2
    assert logs("reopen").count() == 2


# ---------- What a lock freezes ----------


def test_locked_audit_freezes_remarks_and_cancel(raised, locker, anil, admin_client):  # noqa: F811
    client = client_for(anil)
    client.post(reverse("audits:remark_panel", args=[raised.audit_id]), {"text": "Gate open"})
    remark = Remark.objects.get()
    lock(locker, raised.audit)
    add = client.post(reverse("audits:remark_panel", args=[raised.audit_id]), {"text": "Later"})
    assert add.status_code == 403
    edit = client.post(reverse("audits:remark_edit", args=[remark.pk]), {"text": "Changed"})
    assert edit.status_code == 403
    assert client.post(reverse("audits:remark_delete", args=[remark.pk])).status_code == 403
    cancel = admin_client.post(
        reverse("audits:audit_cancel", args=[raised.audit_id]), {"reason": "x" * 10}
    )
    assert cancel.status_code == 403
    assert Remark.objects.get().text == "Gate open"


def test_open_query_stays_workable_after_lock(raised, locker, replier, boss):  # noqa: F811
    lock(locker, raised.audit)
    client_for(replier).post(
        reverse("audits:query_reply", args=[raised.pk]), {"text": "Sent to South"}
    )
    raised.refresh_from_db()
    assert raised.status == QS.REPLY_SUBMITTED
    boss_client = client_for(boss)
    boss_client.post(
        reverse("audits:action_add", args=[raised.pk]),
        {"description": "Raise slip", "owner": replier.pk},
    )
    action = Action.objects.get()
    boss_client.post(reverse("audits:action_step", args=[action.pk, "done"]))
    boss_client.post(reverse("audits:action_step", args=[action.pk, "verify"]))
    boss_client.post(reverse("audits:query_close", args=[raised.pk]), {})
    raised.refresh_from_db()
    assert raised.status == QS.CLOSED


def test_closed_query_of_locked_audit_cannot_reopen(raised, locker, replier, boss, admin_client):  # noqa: F811
    client_for(replier).post(reverse("audits:query_reply", args=[raised.pk]), {"text": "Error"})
    boss_client = client_for(boss)
    boss_client.post(reverse("audits:query_close", args=[raised.pk]), {"note": "Count slip"})
    lock(locker, raised.audit)
    url = reverse("audits:query_reopen", args=[raised.pk])
    assert boss_client.post(url, {"reason": "Look again"}).status_code == 403
    raised.refresh_from_db()
    assert raised.status == QS.CLOSED
    admin_client.post(
        reverse("audits:audit_reopen", args=[raised.audit_id]), {"reason": "Reopen for query"}
    )
    boss_client.post(url, {"reason": "Look again"})
    raised.refresh_from_db()
    assert raised.status == QS.OPEN


def test_open_query_carries_forward_after_lock(raised, locker, priya):  # noqa: F811
    from audits.queries import queries_for_audit

    lock(locker, raised.audit)
    later = Audit.objects.create(
        branch=raised.audit.branch,
        title="Sep26",
        audit_date=raised.audit.audit_date.replace(month=9),
        stock_as_on=raised.audit.audit_date.replace(month=9),
        created_by=priya,
    )
    assert list(queries_for_audit(later)) == [raised]


def test_pending_items_include_locked_audit_queries(raised, locker, replier):  # noqa: F811
    from audits.queries import pending_for

    lock(locker, raised.audit)
    pending = pending_for(replier, raised.audit.branch.company)
    assert list(pending["to_reply"]) == [raised]


# ---------- Media: upload and checks ----------


def test_photo_is_shrunk_to_jpeg(frozen, media_user, media_root):  # noqa: F811
    response = add_media(media_user, "audit", frozen.pk, jpeg("big.png", fmt="PNG"))
    assert response.status_code == 200
    photo = Attachment.objects.get()
    assert photo.kind == Attachment.Kind.PHOTO
    assert photo.content_type == "image/jpeg"
    path = Path(photo.file.path)
    assert path.suffix == ".jpg" and path.stem != "big"
    assert path.parent.parent.parent.name == "attachments"  # attachments/<year>/<month>/
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert max(image.size) == 1600
    assert logs("add attachment").count() == 1


def test_heic_photo_is_converted(frozen, media_user):  # noqa: F811
    out = io.BytesIO()
    Image.new("RGB", (800, 600), "blue").save(out, "HEIF")
    add_media(media_user, "audit", frozen.pk, SimpleUploadedFile("IMG_1.heic", out.getvalue()))
    photo = Attachment.objects.get()
    with Image.open(photo.file.path) as image:
        assert image.format == "JPEG"


def test_several_files_at_once(frozen, media_user):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg(), mp4(), pdf_file(), xlsx_file())
    kinds = sorted(Attachment.objects.values_list("kind", flat=True))
    assert kinds == ["file", "file", "photo", "video"]


@pytest.mark.parametrize(
    "upload, message",
    [
        (lambda: SimpleUploadedFile("tool.exe", b"MZ\x90\x00"), "allowed. Use a photo"),
        (lambda: SimpleUploadedFile("fake.pdf", b"MZ\x90\x00 not a pdf"), "not a real PDF"),
        (lambda: SimpleUploadedFile("fake.mp4", b"just text, no video"), "not a real MP4"),
        (lambda: SimpleUploadedFile("fake.docx", b"PK not really a zip"), "not a real DOCX"),
        (lambda: SimpleUploadedFile("fake.jpg", b"not an image"), "could not be read"),
    ],
)
def test_wrong_or_fake_files_refused(frozen, media_user, upload, message):  # noqa: F811
    response = add_media(media_user, "audit", frozen.pk, upload())
    assert message in response.content.decode()
    assert not Attachment.objects.exists()


def test_one_bad_file_stops_the_whole_upload(frozen, media_user):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg(), SimpleUploadedFile("bad.exe", b"MZ"))
    assert not Attachment.objects.exists()


@pytest.mark.parametrize(
    "setting, upload, what",
    [
        ("PHOTO_MAX_BYTES", jpeg, "A photo"),
        ("VIDEO_MAX_BYTES", mp4, "A video"),
        ("FILE_MAX_BYTES", pdf_file, "A file"),
    ],
)
def test_size_limits(frozen, media_user, settings, setting, upload, what):  # noqa: F811
    setattr(settings, setting, 10)
    response = add_media(media_user, "audit", frozen.pk, upload())
    assert f"{what} can be at most" in response.content.decode()
    assert not Attachment.objects.exists()


def test_line_media_attaches_to_line(frozen, media_user):  # noqa: F811
    line = frozen.lines.get(item_code="A100")
    add_media(media_user, "line", line.pk, jpeg())
    photo = Attachment.objects.get()
    assert photo.line == line and photo.audit == frozen
    assert photo.target_label == "A100"


def test_media_needs_upload_right(frozen, anil, viewer):  # noqa: F811
    assert refused(add_media(anil, "audit", frozen.pk, jpeg()))
    assert refused(add_media(viewer, "audit", frozen.pk, jpeg()))
    assert not Attachment.objects.exists()


def test_query_media_allows_reply_right(raised, replier, boss):  # noqa: F811
    add_media(replier, "query", raised.pk, pdf_file())
    client_for(replier).post(reverse("audits:query_reply", args=[raised.pk]), {"text": "Sent"})
    client_for(boss).post(
        reverse("audits:action_add", args=[raised.pk]), {"description": "Slip", "owner": boss.pk}
    )
    action = Action.objects.get()
    add_media(boss, "action", action.pk, pdf_file("done.pdf"))
    assert Attachment.objects.filter(query=raised, action=None).count() == 1
    assert Attachment.objects.get(action=action).target_label == "A100 · action"
    # The reply right is not enough for audit-level media.
    assert refused(add_media(replier, "audit", raised.audit_id, jpeg()))


def test_media_blocked_in_draft(audit, media_user):  # noqa: F811
    assert refused(add_media(media_user, "audit", audit.pk, jpeg()))


# ---------- Media: remove, report tick, lock ----------


def test_remove_is_soft_and_uploader_or_admin_only(
    frozen,  # noqa: F811
    media_user,
    branches,
    company_a,
    admin_client,
):
    add_media(media_user, "audit", frozen.pk, jpeg(), pdf_file())
    photo, slip = Attachment.objects.order_by("id")
    other = make_user("omar")
    grant(other, branches[0], company_a, role=Role.AUDITOR, upload_media=True)
    remove = "audits:attachment_remove"
    assert refused(client_for(other).post(reverse(remove, args=[photo.pk])))
    client_for(media_user).post(reverse(remove, args=[photo.pk]))
    admin_client.post(reverse(remove, args=[slip.pk]))
    assert not visible_attachments().exists()
    assert Path(Attachment.objects.get(pk=photo.pk).file.path).exists()  # still on disk
    assert logs("remove attachment").count() == 2


def test_report_tick_only_for_photos(frozen, media_user):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg(), pdf_file())
    photo, slip = Attachment.objects.order_by("id")
    client = client_for(media_user)
    client.post(reverse("audits:attachment_report", args=[photo.pk]), {"include": "on"})
    response = client.post(reverse("audits:attachment_report", args=[slip.pk]), {"include": "on"})
    assert "Only photos" in response.content.decode()
    photo.refresh_from_db()
    slip.refresh_from_db()
    assert photo.include_in_report and not slip.include_in_report
    client.post(reverse("audits:attachment_report", args=[photo.pk]), {})
    photo.refresh_from_db()
    assert not photo.include_in_report


def test_lock_freezes_audit_media(raised, media_user, locker, admin_client):  # noqa: F811
    add_media(media_user, "audit", raised.audit_id, jpeg())
    photo = Attachment.objects.get()
    lock(locker, raised.audit)
    assert refused(add_media(media_user, "audit", raised.audit_id, jpeg()))
    client = client_for(media_user)
    assert refused(client.post(reverse("audits:attachment_remove", args=[photo.pk])))
    assert refused(admin_client.post(reverse("audits:attachment_remove", args=[photo.pk])))
    tick = client.post(reverse("audits:attachment_report", args=[photo.pk]), {"include": "on"})
    assert refused(tick)
    photo.refresh_from_db()
    assert not photo.include_in_report


def test_open_query_media_works_after_lock(raised, replier, locker):  # noqa: F811
    lock(locker, raised.audit)
    assert add_media(replier, "query", raised.pk, pdf_file()).status_code == 200
    assert Attachment.objects.filter(query=raised).count() == 1


# ---------- Serving media ----------


def test_serve_checks_branch_right(frozen, media_user, branches, company_a, client):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg(), pdf_file())
    photo, slip = Attachment.objects.order_by("id")
    url = reverse("audits:attachment_file", args=[photo.pk])
    response = client_for(media_user).get(url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    assert "attachment" not in response.get("Content-Disposition", "")
    download = client_for(media_user).get(reverse("audits:attachment_file", args=[slip.pk]))
    assert download["Content-Disposition"].startswith("attachment")

    outsider = make_user("sam")
    grant(outsider, branches[1], company_a, role=Role.AUDITOR, upload_media=True)
    assert client_for(outsider).get(url).status_code == 404
    assert client.get(url).status_code == 302  # not logged in → login page


def test_public_media_url_is_gone(frozen, media_user):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg())
    stored = Attachment.objects.get().file.name
    assert client_for(media_user).get(f"/media/{stored}").status_code == 404


def test_only_logos_and_profile_photos_have_public_media_urls():
    assert resolve("/media/logos/acme.png").kwargs["path"] == "logos/acme.png"
    assert resolve("/media/users/me.jpg").kwargs["path"] == "users/me.jpg"
    with pytest.raises(Resolver404):
        resolve("/media/attachments/2026/11/abc.jpg")


def test_video_range_request(frozen, media_user):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, mp4())
    video = Attachment.objects.get()
    url = reverse("audits:attachment_file", args=[video.pk])
    response = client_for(media_user).get(url, HTTP_RANGE="bytes=0-9")
    assert response.status_code == 206
    assert response["Content-Range"] == f"bytes 0-9/{video.size}"
    assert len(response.content) == 10
    assert client_for(media_user).get(url, HTTP_RANGE="bytes=99999-").status_code == 416


def test_removed_media_not_served(frozen, media_user):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg())
    photo = Attachment.objects.get()
    client = client_for(media_user)
    client.post(reverse("audits:attachment_remove", args=[photo.pk]))
    assert client.get(reverse("audits:attachment_file", args=[photo.pk])).status_code == 404


# ---------- Excel ----------


def test_excel_has_six_sheets_matching_data(raised, locker, anil, replier):  # noqa: F811
    client_for(anil).post(
        reverse("audits:remark_panel", args=[raised.audit_id]), {"text": "=HYPERLINK(bad)"}
    )
    client_for(replier).post(reverse("audits:query_reply", args=[raised.pk]), {"text": "Moved"})
    response = client_for(locker).get(reverse("audits:audit_excel", args=[raised.audit_id]))
    assert response.status_code == 200
    book = load_workbook(io.BytesIO(response.content))
    assert book.sheetnames == [
        "Lines",
        "Remarks",
        "Queries",
        "Actions",
        "Photos and files",
        "Activity log",
    ]
    lines = list(book["Lines"].values)
    assert len(lines) - 1 == raised.audit.lines.count()
    a100 = next(row for row in lines if row[0] == "A100")
    assert a100[3] == 100 and a100[6] == 90 and a100[7] == -10 and a100[9] == "Shortage"
    remarks = list(book["Remarks"].values)
    assert remarks[1][1] == "'=HYPERLINK(bad)"  # formula made harmless
    queries = list(book["Queries"].values)
    assert queries[1][0] == "A100" and queries[1][4] == "Reply Submitted"
    log_actions = {row[2] for row in list(book["Activity log"].values)[1:]}
    assert {"add remark", "reply query"} <= log_actions


def test_excel_activity_only_this_audit(raised, locker, priya, branches):  # noqa: F811
    other = Audit.objects.create(
        branch=branches[0],
        title="Other",
        audit_date=raised.audit.audit_date,
        stock_as_on=raised.audit.stock_as_on,
        created_by=priya,
    )
    ActivityLog.objects.create(
        action="update", record_type="Audit", record_id=str(other.pk), record_label="x"
    )
    response = client_for(locker).get(reverse("audits:audit_excel", args=[raised.audit_id]))
    book = load_workbook(io.BytesIO(response.content))
    ids = {row[4] for row in list(book["Activity log"].values)[1:] if row[3] == "Audit"}
    assert ids == {str(raised.audit_id)}


def test_reports_need_view_right(raised, anil):  # noqa: F811
    client = client_for(anil)  # Count right only
    assert client.get(reverse("audits:audit_excel", args=[raised.audit_id])).status_code == 403
    assert client.get(reverse("audits:audit_pdf", args=[raised.audit_id])).status_code == 403


# ---------- PDF ----------


def pdf_text(response) -> str:
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    return response.content.decode("latin-1")


def test_pdf_draft_until_locked(raised, locker):  # noqa: F811
    url = reverse("audits:audit_pdf", args=[raised.audit_id])
    assert "DRAFT" in pdf_text(client_for(locker).get(url))
    lock(locker, raised.audit)
    text = pdf_text(client_for(locker).get(url, {"stockkeeper": "Kamal Shah"}))
    assert "DRAFT" not in text
    assert "Kamal Shah" in text and "Lata" in text and "Anil" in text  # signatures
    assert "Not counted \\(1 lines\\)" in text  # A300 was never counted
    assert "Signatures" in text


def test_pdf_only_ticked_photos(raised, media_user, locker):  # noqa: F811
    add_media(media_user, "audit", raised.audit_id, jpeg("one.jpg"), jpeg("two.jpg"))
    first, _second = Attachment.objects.order_by("id")
    client_for(media_user).post(
        reverse("audits:attachment_report", args=[first.pk]), {"include": "on"}
    )
    text = pdf_text(client_for(locker).get(reverse("audits:audit_pdf", args=[raised.audit_id])))
    assert "Photos \\(1\\)" in text
    assert text.count("/Subtype /Image") == 1


def test_pdf_not_in_draft(audit, locker):  # noqa: F811
    response = client_for(locker).get(reverse("audits:audit_pdf", args=[audit.pk]))
    assert response.status_code == 403


# ---------- Fixes from the Phase 5 code review ----------


def test_too_many_files_gives_clear_message(frozen, media_user, settings):  # noqa: F811
    settings.ATTACHMENT_MAX_FILES = 2
    response = add_media(
        media_user, "audit", frozen.pk, jpeg("a.jpg"), jpeg("b.jpg"), jpeg("c.jpg")
    )
    assert "Pick at most 2 files" in response.content.decode()
    assert not Attachment.objects.exists()


def test_huge_photo_refused_before_decoding(frozen, media_user, settings):  # noqa: F811
    settings.PHOTO_MAX_MEGAPIXELS = 1  # the 3000 × 2000 test photo is 6 MP
    response = add_media(media_user, "audit", frozen.pk, jpeg())
    assert "at most 1 megapixels" in response.content.decode()
    assert not Attachment.objects.exists()


def test_failed_save_leaves_no_files_on_disk(frozen, media_user, media_root, monkeypatch):  # noqa: F811
    from audits import media

    calls = []

    def log_then_fail(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(media, "log_activity", log_then_fail)
    with pytest.raises(RuntimeError):
        media.save_attachments(media_user, [jpeg("a.jpg"), pdf_file()], audit=frozen)
    assert not Attachment.objects.exists()
    assert not [path for path in media_root.rglob("*") if path.is_file()]


def test_report_tick_frozen_after_lock_even_on_open_query(raised, replier, locker):  # noqa: F811
    add_media(replier, "query", raised.pk, jpeg())
    photo = Attachment.objects.get()
    lock(locker, raised.audit)
    client = client_for(replier)
    tick = client.post(reverse("audits:attachment_report", args=[photo.pk]), {"include": "on"})
    assert refused(tick)
    photo.refresh_from_db()
    assert not photo.include_in_report
    # The open query can still get new files after lock.
    assert not refused(add_media(replier, "query", raised.pk, pdf_file()))


def test_excel_formula_guard_matches_csv():
    from audits.reports import _cell
    from core.datatable import formula_safe

    assert formula_safe("=SUM(A1)") == "'=SUM(A1)"
    assert formula_safe("-5") == "-5"  # a plain number stays a number
    assert _cell("- bin mismatch") == formula_safe("- bin mismatch") == "'- bin mismatch"


# ---------- Notes on photos and files ----------


def test_upload_note_goes_on_every_file(frozen, media_user):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg(), pdf_file(), note="  Damaged box,\n rack B3 ")
    assert set(Attachment.objects.values_list("note", flat=True)) == {"Damaged box, rack B3"}
    assert logs("add attachment").first().changes["note"] == [None, "Damaged box, rack B3"]


def test_too_long_note_refused(frozen, media_user):  # noqa: F811
    page = add_media(media_user, "audit", frozen.pk, jpeg(), note="x" * 201).content.decode()
    assert "Keep the note to 200 characters" in page
    assert not Attachment.objects.exists()


def test_note_can_be_changed_by_uploader_only(frozen, media_user, anil):  # noqa: F811
    add_media(media_user, "audit", frozen.pk, jpeg())
    photo = Attachment.objects.get()
    url = reverse("audits:attachment_note", args=[photo.pk])
    page = client_for(media_user).post(url, {"note": "Wet cartons"}).content.decode()
    photo.refresh_from_db()
    assert photo.note == "Wet cartons" and 'value="Wet cartons"' in page
    assert logs("attachment note").get().changes["note"] == ["", "Wet cartons"]

    client_for(anil).post(url, {"note": "Changed by someone else"})
    photo.refresh_from_db()
    assert photo.note == "Wet cartons"


def test_note_frozen_after_lock(raised, media_user, locker):  # noqa: F811
    add_media(media_user, "audit", raised.audit_id, jpeg(), note="Before lock")
    photo = Attachment.objects.get()
    lock(locker, raised.audit)
    client_for(media_user).post(
        reverse("audits:attachment_note", args=[photo.pk]), {"note": "After lock"}
    )
    photo.refresh_from_db()
    assert photo.note == "Before lock"


def test_note_in_pdf_and_excel(raised, media_user, locker):  # noqa: F811
    add_media(media_user, "audit", raised.audit_id, jpeg(), note="Damaged box, rack B3")
    photo = Attachment.objects.get()
    client_for(media_user).post(
        reverse("audits:attachment_report", args=[photo.pk]), {"include": "on"}
    )
    client = client_for(locker)
    assert "Damaged box, rack B3" in pdf_text(
        client.get(reverse("audits:audit_pdf", args=[raised.audit_id]))
    )
    response = client.get(reverse("audits:audit_excel", args=[raised.audit_id]))
    rows = list(load_workbook(io.BytesIO(response.content))["Photos and files"].values)
    assert rows[1][:4] == ("General", "Photo", "Damaged box, rack B3", "shelf.jpg")
