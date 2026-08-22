"""Value objects exchanged with ComfyUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OutputKind(str, Enum):
    IMAGE = "images"
    GIF = "gifs"
    VIDEO = "videos"
    AUDIO = "audio"


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
