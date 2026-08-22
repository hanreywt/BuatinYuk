"""Upload validation - the boundary where untrusted bytes reach the filesystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.uploads import (
    MAX_UPLOAD_BYTES,
    UploadRejected,
    UploadService,
    detect_image_type,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 200
GIF = b"GIF89a" + b"\x00" * 200


class FakeComfy:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, int]] = []

    async def upload_image(self, data: bytes, filename: str) -> str:
        self.uploads.append((filename, len(data)))
        return filename


@pytest.fixture
def service(tmp_path: Path):
    return UploadService(comfy=FakeComfy(), input_dir=tmp_path / "inputs")


# ---------------- format detection ----------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, ".png"), (JPEG, ".jpg"), (WEBP, ".webp"), (GIF, ".gif")],
)
def test_recognises_supported_formats(data: bytes, expected: str) -> None:
    assert detect_image_type(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"MZ\x90\x00" + b"\x00" * 200,          # a Windows executable
        b"#!/bin/sh\nrm -rf /" + b" " * 200,     # a shell script
        b"<?php system($_GET[0]); ?>" + b" " * 200,
        b"%PDF-1.4" + b"\x00" * 200,
        b"PK\x03\x04" + b"\x00" * 200,          # a zip
    ],
)
def test_rejects_non_images_whatever_they_claim_to_be(service, data: bytes) -> None:
    """The filename and the reported MIME type are attacker-controlled; bytes are not."""
    assert detect_image_type(data) is None
    with pytest.raises(UploadRejected, match="signature"):
        service.validate(data)


def test_a_renamed_executable_is_still_rejected(service) -> None:
    """Calling it cat.png does not make it a PNG."""
    with pytest.raises(UploadRejected):
        service.validate(b"MZ\x90\x00" + b"\x00" * 500)


# ---------------- size ----------------


def test_empty_and_tiny_files_are_rejected(service) -> None:
    with pytest.raises(UploadRejected, match="too small"):
        service.validate(b"")
    with pytest.raises(UploadRejected, match="too small"):
        service.validate(PNG[:10])


def test_oversized_upload_is_rejected(service) -> None:
    with pytest.raises(UploadRejected) as exc:
        service.validate(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_UPLOAD_BYTES + 1))
    assert "too large" in exc.value.user_message


def test_a_file_at_the_limit_is_accepted(service) -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_UPLOAD_BYTES - 8)
    assert service.validate(data) == ".png"


# ---------------- storage ----------------


async def test_store_keeps_a_local_copy_under_the_owners_directory(service, tmp_path) -> None:
    stored = await service.store(PNG, owner_id=777, job_reference="job_000005_u777")

    assert stored.local_path.exists()
    assert stored.local_path.read_bytes() == PNG
    assert "user_777" in stored.local_path.parts
    assert (tmp_path / "inputs").resolve() in stored.local_path.resolve().parents


async def test_stored_name_comes_from_the_job_not_the_sender(service) -> None:
    """Nothing the sender chose may influence where the file lands."""
    stored = await service.store(PNG, owner_id=777, job_reference="job_000005_u777")
    assert stored.local_path.name == "job_000005_u777.png"


@pytest.mark.parametrize(
    "hostile_reference",
    ["../../.env", "..\\..\\windows\\system32\\evil", "/etc/passwd", "a/b/c"],
)
async def test_a_hostile_job_reference_cannot_escape_the_input_directory(
    tmp_path: Path, hostile_reference: str
) -> None:
    """Defence in depth: the reference is ours, but it is still sanitised."""
    service = UploadService(comfy=FakeComfy(), input_dir=tmp_path / "inputs")
    stored = await service.store(PNG, owner_id=1, job_reference=hostile_reference)
    assert (tmp_path / "inputs").resolve() in stored.local_path.resolve().parents


async def test_store_hands_the_file_to_comfyui(service) -> None:
    stored = await service.store(JPEG, owner_id=5, job_reference="job_1_u5")
    assert service._comfy.uploads == [("job_1_u5.jpg", len(JPEG))]
    assert stored.comfy_reference == "job_1_u5.jpg"


async def test_a_rejected_file_never_reaches_disk_or_comfyui(service, tmp_path) -> None:
    with pytest.raises(UploadRejected):
        await service.store(b"MZ\x90\x00" + b"\x00" * 200, owner_id=5, job_reference="job_1_u5")

    assert service._comfy.uploads == []
    assert not (tmp_path / "inputs").exists() or not list(
        (tmp_path / "inputs").rglob("*.*")
    )
