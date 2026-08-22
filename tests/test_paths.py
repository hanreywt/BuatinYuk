"""Path safety: the boundary between untrusted text and the filesystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.paths import (
    PathSafetyError,
    assert_within,
    is_within,
    safe_join,
    sanitize_name,
)


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "/absolute/path",
        "C:\\Users\\user\\.env",
        "a/b/c",
        "....//....//x",
        "foo\x00bar",
    ],
)
def test_sanitize_removes_every_separator_and_traversal(raw: str) -> None:
    result = sanitize_name(raw)
    assert "/" not in result
    assert "\\" not in result
    assert ".." not in result
    assert "\x00" not in result
    assert not result.startswith(".")


@pytest.mark.parametrize("raw", ["", "   ", "...", "___", "///", "CON", "nul.txt", "LPT1"])
def test_sanitize_never_returns_an_unusable_or_reserved_name(raw: str) -> None:
    assert sanitize_name(raw) == "untitled"


def test_sanitize_truncates_long_names() -> None:
    assert len(sanitize_name("x" * 500)) <= 60


def test_sanitize_keeps_readable_text() -> None:
    assert sanitize_name("Jakarta_night-01.png") == "Jakarta_night-01.png"
    assert sanitize_name("naïve café") == "naive_cafe"


def test_safe_join_stays_inside_root(tmp_path: Path) -> None:
    assert is_within(safe_join(tmp_path, "user_1", "job_2.png"), tmp_path)


@pytest.mark.parametrize(
    "attack",
    ["../secrets", "..\\..\\.env", "/etc/shadow", "....//....//root", "C:\\Windows\\System32"],
)
def test_safe_join_neutralises_traversal_attempts(tmp_path: Path, attack: str) -> None:
    """Traversal collapses into a harmless name; it must never leave the root."""
    assert is_within(safe_join(tmp_path, attack), tmp_path)


def test_safe_join_refuses_when_nothing_usable_remains(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError):
        safe_join(tmp_path)


def test_is_within_rejects_siblings_and_parents(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    root.mkdir()
    assert not is_within(tmp_path / ".env", root)
    assert not is_within(tmp_path / "outputs_other" / "x.png", root)
    assert not is_within(root.parent, root)
    assert is_within(root / "a" / "b.png", root)


def test_assert_within_raises_for_outside_paths(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    root.mkdir()
    assert assert_within(root / "ok.png", root) == root / "ok.png"
    with pytest.raises(PathSafetyError):
        assert_within(tmp_path / ".env", root)


def test_comfy_supplied_filename_cannot_escape_the_output_dir(tmp_path: Path) -> None:
    """ComfyUI reports filenames and subfolders; they are outside our control."""
    hostile = safe_join(tmp_path, "../../..", "evil.png")
    assert is_within(hostile, tmp_path)
