"""Accepting images from Telegram.

An uploaded file is untrusted in a way text is not: it is bytes chosen by the sender
that end up on disk and are then read by another program. Three things are checked
before anything reaches ComfyUI:

1. **Size** - refused above a cap, so a large send cannot fill the disk.
2. **Format** - the leading bytes must actually be a supported image. The filename and
   the MIME type Telegram reports are both attacker-controlled and are not trusted.
3. **Name** - the stored name is built from the job's own identifiers, never from what
   the sender called the file.

The local copy is kept under this application's own input directory so an upload is
attributable to its owner, and a copy is handed to ComfyUI for the graph to load.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.comfy.client import ComfyUIClient
from app.utils.logging import get_logger
from app.utils.paths import safe_join

log = get_logger(__name__)

#: Telegram itself caps bot downloads at 20 MB; this is the stricter local limit.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MIN_UPLOAD_BYTES = 64

#: Leading bytes -> extension. Deliberately a small allowlist of formats ComfyUI reads.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


class UploadRejected(Exception):
    """The file cannot be accepted. `user_message` is safe to show."""

    def __init__(self, message: str, user_message: str) -> None:
        super().__init__(message)
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class StoredUpload:
    """An accepted image: kept locally, and known to ComfyUI by `comfy_reference`."""

    local_path: Path
    comfy_reference: str
    size_bytes: int
    extension: str


def detect_image_type(data: bytes) -> str | None:
    """Return the extension implied by the file's own leading bytes, or None.

    WebP needs a second check because its signature is split across two ranges.
    """
    for signature, extension in _SIGNATURES:
        if data.startswith(signature):
            return extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


class UploadService:
    def __init__(self, *, comfy: ComfyUIClient, input_dir: Path) -> None:
        self._comfy = comfy
        self._input_dir = input_dir

    def validate(self, data: bytes) -> str:
        """Check size and format. Returns the extension; raises UploadRejected."""
        if len(data) < MIN_UPLOAD_BYTES:
            raise UploadRejected(
                f"upload of {len(data)} bytes is too small to be an image",
                "That file is empty or too small to be an image.",
            )
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadRejected(
                f"upload of {len(data)} bytes exceeds {MAX_UPLOAD_BYTES}",
                f"That image is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
            )

        extension = detect_image_type(data)
        if extension is None:
            # The name and the reported MIME type are not evidence; the bytes are.
            raise UploadRejected(
                "upload did not match any supported image signature",
                "That does not look like an image I can read. Send a PNG or a JPEG.",
            )
        return extension

    async def store(self, data: bytes, *, owner_id: int, job_reference: str) -> StoredUpload:
        """Validate, keep a local copy, and hand it to ComfyUI.

        `job_reference` should be derived from identifiers this application controls -
        never from the sender's filename or caption.
        """
        extension = self.validate(data)

        # Name and location come from our own identifiers only.
        local_path = safe_join(
            self._input_dir, f"user_{owner_id}", f"{job_reference}{extension}"
        )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)

        reference = await self._comfy.upload_image(data, f"{job_reference}{extension}")

        log.info(
            "upload.stored",
            owner_id=owner_id,
            bytes=len(data),
            extension=extension,
            reference=reference,
        )
        return StoredUpload(
            local_path=local_path,
            comfy_reference=reference,
            size_bytes=len(data),
            extension=extension,
        )
