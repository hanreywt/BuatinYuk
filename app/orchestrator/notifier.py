"""How the worker talks back to whoever submitted a job.

The worker must not import Telegram. It reports through this protocol, so the queue
can be driven from a test, a script, or a future interface without change - and so a
Telegram outage cannot take the GPU worker down with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.jobs.models import Job


@runtime_checkable
class JobNotifier(Protocol):
    """Implementations must never raise: delivery is best-effort by contract.

    A failed notification is logged and swallowed. Losing a status message is a small
    problem; losing the GPU worker to an unhandled exception is a large one.
    """

    async def job_started(self, job: Job) -> None:
        """The job has reached the front of the queue and work has begun."""

    async def job_progress(self, job: Job, message: str) -> None:
        """A human-readable progress update. Called sparingly, not per step."""

    async def job_completed(self, job: Job, files: list[Path]) -> None:
        """The job finished. `files` are local paths this application owns."""

    async def job_failed(self, job: Job, user_message: str) -> None:
        """The job failed. `user_message` is already safe to show a user."""


class NullNotifier:
    """Discards every notification. Useful in tests and for headless runs."""

    async def job_started(self, job: Job) -> None:
        return None

    async def job_progress(self, job: Job, message: str) -> None:
        return None

    async def job_completed(self, job: Job, files: list[Path]) -> None:
        return None

    async def job_failed(self, job: Job, user_message: str) -> None:
        return None
