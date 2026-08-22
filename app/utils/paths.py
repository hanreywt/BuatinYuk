"""Filesystem safety.

Telegram text must never become a path. Everything a user influences passes through
here first: names are sanitised to a strict charset, and every resolved path is proven
to sit inside a directory this application owns.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

MAX_STEM_LENGTH = 60

# Deliberately strict: ASCII word characters, dash, dot. Nothing else survives.
_ALLOWED = re.compile(r"[^A-Za-z0-9._-]+")
_COLLAPSE = re.compile(r"[-_]{2,}")

# Reserved device names on Windows; a file called "CON.png" is a trap.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class PathSafetyError(Exception):
    """A path escaped its permitted directory, or a name could not be made safe."""


def sanitize_name(raw: str, *, fallback: str = "untitled") -> str:
    """Reduce arbitrary text to a safe single path component (no directories).

    Never returns an empty string, a reserved device name, a dotfile, or anything
    containing a separator.
    """
    text = unicodedata.normalize("NFKD", raw)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _ALLOWED.sub("_", text)
    text = _COLLAPSE.sub("_", text).strip("._-")
    text = text[:MAX_STEM_LENGTH].strip("._-")

    if not text or text.upper().split(".")[0] in _WINDOWS_RESERVED:
        return fallback
    return text


def is_within(candidate: Path, root: Path) -> bool:
    """True only if `candidate` resolves inside `root`. Symlink- and `..`-proof."""
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (ValueError, OSError):
        return False
    return True


def safe_join(root: Path, *parts: str) -> Path:
    """Join user-influenced components under `root`, refusing any escape.

    Each part is sanitised individually, so a part containing separators or `..`
    cannot introduce a new directory level.
    """
    safe_parts = [sanitize_name(p) for p in parts if p]
    if not safe_parts:
        raise PathSafetyError("no usable path components after sanitisation")

    result = root.joinpath(*safe_parts)
    if not is_within(result, root):
        raise PathSafetyError("refusing to build a path outside the permitted directory")
    return result


def assert_within(candidate: Path, root: Path) -> Path:
    """Return `candidate` if it is inside `root`, else raise.

    Use this on any path derived from an external system's response - ComfyUI tells
    us a filename and a subfolder, and both are outside our control.
    """
    if not is_within(candidate, root):
        raise PathSafetyError("path is outside the permitted directory")
    return candidate
