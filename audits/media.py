"""Checking and saving attachments (photos, videos, files). Views only call these.

Type is chosen by the file extension and then confirmed from the file's content, so a
renamed .exe can't pass as a .pdf. Photos are turned into a JPEG of at most 1600 px.
"""

import io
import logging
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from audits.models import Attachment
from core.activity import log_activity

register_heif_opener()  # lets Pillow read iPhone HEIC photos
logger = logging.getLogger(__name__)

Kind = Attachment.Kind

PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
FILE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
ACCEPT = ",".join(sorted(PHOTO_TYPES | set(VIDEO_TYPES) | set(FILE_TYPES)))


class MediaError(Exception):
    """The file was refused; the message is shown to the user."""


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB"


def kind_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in PHOTO_TYPES:
        return Kind.PHOTO
    if ext in VIDEO_TYPES:
        return Kind.VIDEO
    if ext in FILE_TYPES:
        return Kind.FILE
    raise MediaError(
        f"“{name}”: this file type isn't allowed. Use a photo (JPG, PNG, HEIC), "
        "a video (MP4, MOV, WEBM) or a PDF / DOCX / XLSX file."
    )


def _check_size(name: str, size: int, limit: int, what: str):
    if size > limit:
        raise MediaError(
            f"“{name}” is {size / (1024 * 1024):.1f} MB. {what} can be at most {_mb(limit)}."
        )


def _photo(upload) -> tuple[ContentFile, str]:
    try:
        with Image.open(upload) as image:
            if image.format not in ("JPEG", "PNG", "HEIF"):
                raise MediaError(f"“{upload.name}” is not a JPG, PNG or HEIC photo.")
            # Size is read from the header, before the pixels are decoded into memory.
            megapixels = image.width * image.height / 1_000_000
            if megapixels > settings.PHOTO_MAX_MEGAPIXELS:
                logger.info("Photo %s refused: %.0f MP", upload.name, megapixels)
                raise MediaError(
                    f"“{upload.name}” is {image.width} × {image.height} px. "
                    f"A photo can be at most {settings.PHOTO_MAX_MEGAPIXELS} megapixels."
                )
            image.draft("RGB", (settings.PHOTO_MAX_PIXELS, settings.PHOTO_MAX_PIXELS))  # JPEG only
            image = ImageOps.exif_transpose(image)  # phone photos taken sideways
            image.thumbnail((settings.PHOTO_MAX_PIXELS, settings.PHOTO_MAX_PIXELS))
            if image.mode != "RGB":
                image = image.convert("RGB")
            out = io.BytesIO()
            image.save(out, "JPEG", quality=85, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        logger.info("Photo %s refused: %s", upload.name, error)
        raise MediaError(f"“{upload.name}” could not be read as a photo.") from error
    return ContentFile(out.getvalue(), name=f"{Path(upload.name).stem}.jpg"), "image/jpeg"


def _head(upload, size: int = 16) -> bytes:
    upload.seek(0)
    head = upload.read(size)
    upload.seek(0)
    return head


def _video(upload) -> tuple:
    head = _head(upload)
    ext = Path(upload.name).suffix.lower()
    is_mp4_family = head[4:8] == b"ftyp"  # MP4 and MOV
    is_webm = head[:4] == b"\x1a\x45\xdf\xa3"
    if (ext == ".webm" and not is_webm) or (ext != ".webm" and not is_mp4_family):
        raise MediaError(f"“{upload.name}” is not a real {ext[1:].upper()} video.")
    return upload, VIDEO_TYPES[ext]


def _office_file(upload) -> tuple:
    ext = Path(upload.name).suffix.lower()
    if ext == ".pdf":
        ok = _head(upload, 5) == b"%PDF-"
    else:
        folder = "word/" if ext == ".docx" else "xl/"
        try:
            with zipfile.ZipFile(upload) as archive:
                ok = any(name.startswith(folder) for name in archive.namelist())
        except zipfile.BadZipFile:
            ok = False
        upload.seek(0)
    if not ok:
        raise MediaError(f"“{upload.name}” is not a real {ext[1:].upper()} file.")
    return upload, FILE_TYPES[ext]


def check_upload(upload) -> tuple[str, object, str]:
    """(kind, content to store, content type) — or MediaError with a clear message."""
    kind = kind_for(upload.name)
    if kind == Kind.PHOTO:
        _check_size(upload.name, upload.size, settings.PHOTO_MAX_BYTES, "A photo")
        content, content_type = _photo(upload)
    elif kind == Kind.VIDEO:
        _check_size(upload.name, upload.size, settings.VIDEO_MAX_BYTES, "A video")
        content, content_type = _video(upload)
    else:
        _check_size(upload.name, upload.size, settings.FILE_MAX_BYTES, "A file")
        content, content_type = _office_file(upload)
    return kind, content, content_type


NOTE_MAX = Attachment._meta.get_field("note").max_length


def clean_note(note: str) -> str:
    note = " ".join((note or "").split())  # one line, no extra spaces
    if len(note) > NOTE_MAX:
        raise MediaError(f"Keep the note to {NOTE_MAX} characters (it has {len(note)}).")
    return note


def save_attachments(actor, uploads, *, audit, line=None, query=None, action=None, note=""):
    """Check every file first; save all of them only if none is refused.
    The note (e.g. “Damaged box in rack B3”) goes on every file of this upload."""
    note = clean_note(note)
    if not uploads:
        raise MediaError("Choose at least one file.")
    if len(uploads) > settings.ATTACHMENT_MAX_FILES:
        raise MediaError(f"Pick at most {settings.ATTACHMENT_MAX_FILES} files at a time.")
    checked = [(upload, *check_upload(upload)) for upload in uploads]
    saved = []
    try:
        with transaction.atomic():
            for upload, kind, content, content_type in checked:
                attachment = Attachment(
                    audit=audit,
                    line=line,
                    query=query,
                    action=action,
                    kind=kind,
                    original_name=upload.name[:255],
                    note=note,
                    content_type=content_type,
                    size=content.size,
                    uploaded_by=actor,
                )
                attachment.file.save(content.name or upload.name, content, save=False)
                saved.append(attachment)
                attachment.save()
                log_activity(
                    actor,
                    "add attachment",
                    attachment,
                    {
                        "file": [None, attachment.original_name],
                        "where": [None, attachment.target_label],
                        "note": [None, note],
                    },
                )
    except Exception:
        # The rows were rolled back; don't leave their files behind on disk.
        logger.exception("Saving attachments failed; removing %s written file(s)", len(saved))
        for attachment in saved:
            attachment.file.storage.delete(attachment.file.name)
        raise
    return saved


def remove_attachment(actor, attachment: Attachment) -> None:
    """Hide it; the file stays on disk and the log keeps who removed it."""
    attachment.is_deleted = True
    attachment.deleted_by = actor
    attachment.deleted_at = timezone.now()
    attachment.save(update_fields=["is_deleted", "deleted_by", "deleted_at"])
    log_activity(actor, "remove attachment", attachment, {"file": [attachment.original_name, None]})


def set_note(actor, attachment: Attachment, note: str) -> None:
    note = clean_note(note)
    if attachment.note != note:
        old_note = attachment.note
        attachment.note = note
        attachment.save(update_fields=["note"])
        log_activity(actor, "attachment note", attachment, {"note": [old_note, note]})


def set_in_report(actor, attachment: Attachment, include: bool) -> None:
    if attachment.kind != Kind.PHOTO:
        raise MediaError("Only photos can be printed in the report.")
    if attachment.include_in_report != include:
        attachment.include_in_report = include
        attachment.save(update_fields=["include_in_report"])
        log_activity(
            actor, "photo in report", attachment, {"include_in_report": [not include, include]}
        )
