from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from telegram_project_manager.platform.router import IncomingAttachment
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError


SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif"})
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10_000_000
MAX_TOTAL_IMAGE_BYTES = 20_000_000
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
}


def validate_attachments(attachments: Sequence[IncomingAttachment]) -> None:
    if len(attachments) > MAX_IMAGES:
        raise ValueError(f"Too many images. Maximum: {MAX_IMAGES}.")
    total_size = 0
    for attachment in attachments:
        if attachment.mime_type not in SUPPORTED_IMAGE_TYPES:
            raise ValueError(f"Unsupported image type: {attachment.mime_type}.")
        if attachment.file_size > MAX_IMAGE_BYTES:
            raise ValueError("Each image must be 10 MB or smaller.")
        total_size += max(0, attachment.file_size)
    if total_size > MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("Images must be 20 MB or smaller in total.")


def stage_attachments(
    *,
    bot: TelegramBotApi,
    attachments: Sequence[IncomingAttachment],
    destination: Path,
) -> tuple[str, ...]:
    validate_attachments(attachments)
    if not attachments:
        return ()

    _create_staging_directory(destination)
    total_size = 0
    paths: list[str] = []
    for position, attachment in enumerate(attachments, 1):
        try:
            content = bot.download_file(attachment.file_id, MAX_IMAGE_BYTES)
        except TelegramBotApiError as exc:
            raise ValueError("Telegram image download failed.") from exc
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("Each image must be 10 MB or smaller.")
        total_size += len(content)
        if total_size > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("Images must be 20 MB or smaller in total.")
        _validate_image(content, attachment.mime_type)
        extension = MIME_EXTENSIONS[attachment.mime_type]
        path = destination / f"{position}.{extension}"
        path.write_bytes(content)
        path.chmod(0o600)
        paths.append(str(path.resolve()))
    return tuple(paths)


def _create_staging_directory(destination: Path) -> None:
    parent = destination.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValueError("Image staging path is unavailable.")
    parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise ValueError("Image staging path is unavailable.") from exc
    destination.chmod(0o700)


def _validate_image(content: bytes, mime_type: str) -> None:
    header = content[:16].hex()
    valid = {
        "image/jpeg": header.startswith("ffd8ff"),
        "image/png": header.startswith("89504e470d0a1a0a"),
        "image/gif": header.startswith(("474946383761", "474946383961")),
    }
    if not valid.get(mime_type, False):
        raise ValueError(f"Downloaded image is not valid {mime_type}.")
