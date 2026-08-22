"""Value objects exchanged with ComfyUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OutputKind(str, Enum):
    IMAGE = "images"
    GIF = "gifs"
    VIDEO = "videos"
    AUDIO = "audio"


#: Which media kind a file extension represents. ComfyUI's own grouping is not
#: reliable for this - SaveVideo reports its .mp4 under the "images" key - so the
#: extension is what delivery and storage decide from.
VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv", ".gif"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".ogg"})


def kind_for_suffix(suffix: str) -> "OutputKind":
    """Classify a produced file by its extension."""
    lowered = suffix.lower()
    if lowered in VIDEO_SUFFIXES:
        return OutputKind.VIDEO
    if lowered in AUDIO_SUFFIXES:
        return OutputKind.AUDIO
    return OutputKind.IMAGE


@dataclass(frozen=True, slots=True)
class OutputRef:
    """A file ComfyUI says it produced. All three fields come from ComfyUI, not the
    user, but they still address a filesystem - treat them as untrusted input."""

    filename: str
    subfolder: str
    type: str  # "output" | "temp" | "input"
    kind: OutputKind = OutputKind.IMAGE

    @property
    def query(self) -> dict[str, str]:
        return {"filename": self.filename, "subfolder": self.subfolder, "type": self.type}

    @property
    def actual_kind(self) -> "OutputKind":
        """The kind implied by the filename, which outranks ComfyUI's grouping."""
        from pathlib import PurePosixPath

        return kind_for_suffix(PurePosixPath(self.filename).suffix)


@dataclass(frozen=True, slots=True)
class QueueState:
    running: int
    pending: int

    @property
    def busy(self) -> bool:
        return self.running > 0

    @property
    def total(self) -> int:
        return self.running + self.pending


@dataclass(frozen=True, slots=True)
class Progress:
    """Best-effort execution progress. Absent fields mean ComfyUI has not said yet."""

    node: str | None = None
    step: int | None = None
    total_steps: int | None = None
    queue_remaining: int | None = None

    @property
    def percent(self) -> int | None:
        if self.step is None or not self.total_steps:
            return None
        return min(100, round(100 * self.step / self.total_steps))


@dataclass(frozen=True, slots=True)
class SystemStatus:
    online: bool
    version: str | None = None
    devices: list[str] = field(default_factory=list)
    vram_free_gb: float | None = None
    vram_total_gb: float | None = None
    queue: QueueState | None = None
    error: str | None = None
