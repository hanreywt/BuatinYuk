"""Job records and the state machine they move through.

A job is the unit of accountability: it ties a Telegram request to a workflow, to
whatever ComfyUI did with it, and to the files that came back. Every job belongs to
exactly one Telegram user, and that ownership is what stops one user's history or
"upscale that last one" from ever resolving to somebody else's output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    RECEIVED = "received"      # accepted from Telegram, not yet queued
    QUEUED = "queued"          # waiting for the single GPU worker
    PREPARING = "preparing"    # building and validating the graph
    GENERATING = "generating"  # submitted to ComfyUI, running
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


_TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})

#: Every legal move. Anything absent here is a bug, and the repository refuses it
#: rather than silently corrupting the queue.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.RECEIVED: frozenset({JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.QUEUED: frozenset({JobStatus.PREPARING, JobStatus.FAILED, JobStatus.CANCELLED}),
    # PREPARING may return to QUEUED: nothing has been sent to ComfyUI at that point, so
    # requeueing after a restart cannot duplicate GPU work.
    JobStatus.PREPARING: frozenset(
        {JobStatus.GENERATING, JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    # GENERATING deliberately cannot return to QUEUED. Once a graph is with ComfyUI,
    # requeueing risks running the same expensive job twice; recovery resolves such a
    # job by asking ComfyUI what happened instead.
    JobStatus.GENERATING: frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    """An illegal status change was attempted."""

    def __init__(self, job_id: int, current: JobStatus, requested: JobStatus) -> None:
        super().__init__(
            f"job {job_id}: cannot move from {current.value} to {requested.value}"
        )
        self.job_id = job_id
        self.current = current
        self.requested = requested


def can_transition(current: JobStatus, requested: JobStatus) -> bool:
    return requested in ALLOWED_TRANSITIONS[current]


@dataclass(slots=True)
class JobOutput:
    """One file produced by a job, stored under this application's output directory."""

    job_id: int
    path: Path
    kind: str = "image"
    size_bytes: int | None = None
    id: int | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class Job:
    telegram_user_id: int
    telegram_chat_id: int
    original_request: str
    workflow_id: str

    id: int | None = None
    telegram_message_id: int | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.RECEIVED
    comfy_prompt_id: str | None = None

    #: Full detail, for logs and admins only. Never sent to a user.
    error_message: str | None = None
    #: The safe sentence a user is allowed to see.
    user_message: str | None = None

    retry_count: int = 0
    cancel_requested: bool = False

    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    outputs: list[JobOutput] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()

    def owned_by(self, telegram_user_id: int) -> bool:
        return self.telegram_user_id == telegram_user_id

    def output_prefix(self) -> str:
        """The ComfyUI filename prefix for this job.

        Derived only from integers we control, so it can never carry user text into a
        filename. Includes the owner so stray files remain attributable.
        """
        return f"job_{self.id or 0:06d}_u{self.telegram_user_id}"

    def summary(self) -> str:
        """One-line description for logs and admin views. Contains no file paths."""
        return (
            f"job#{self.id} user={self.telegram_user_id} workflow={self.workflow_id} "
            f"status={self.status.value} outputs={len(self.outputs)}"
        )

    # ---------------- serialisation ----------------

    def parameters_json(self) -> str:
        return json.dumps(self.parameters, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def parse_parameters(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
