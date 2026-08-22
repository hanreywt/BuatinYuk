"""The orchestrator: the only route from a request to a queued job.

Everything a request must pass happens here, in this order and in code - never by
model reasoning:

    authorise -> check quota -> resolve workflow -> validate parameters -> create job
    -> queue it

Interpreting *what the user meant* (Phase 5, Claude) produces a `GenerationRequest`.
It does not get to skip any of the steps above; it only fills in the request.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

from app.comfy.client import ComfyUIClient
from app.jobs.models import Job, JobStatus
from app.jobs.repository import JobRepository
from app.orchestrator.worker import GenerationWorker
from app.users.models import User
from app.users.service import UserService
from app.utils.logging import get_logger
from app.workflows.registry import ParameterError, WorkflowRegistry, WorkflowSpec

log = get_logger(__name__)

MAX_REQUEST_LENGTH = 2000


@dataclass(slots=True)
class GenerationRequest:
    """A fully-formed generation intent, whatever produced it."""

    telegram_user_id: int
    telegram_chat_id: int
    text: str
    telegram_message_id: int | None = None
    workflow_id: str | None = None  # None means the configured default
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Accepted:
    """The outcome of a successful submission, ready to report back."""

    job: Job
    queue_position: int | None
    ahead_of_it: int

    def describe(self) -> str:
        if self.queue_position and self.queue_position > 1:
            return (
                f"Job #{self.job.id} accepted.\nQueue position: {self.queue_position}."
            )
        return f"Job #{self.job.id} accepted. Starting now."


@dataclass(slots=True)
class SystemStatus:
    comfyui_online: bool
    comfyui_version: str | None
    worker_running: bool
    worker_busy: bool
    current_job_id: int | None
    queue_length: int
    vram_free_gb: float | None
    vram_total_gb: float | None
    workflows: list[str]

    def describe(self) -> str:
        lines = [
            f"Generator : {'online' if self.comfyui_online else 'OFFLINE'}"
            + (f" (ComfyUI {self.comfyui_version})" if self.comfyui_version else ""),
            f"Worker    : {'busy' if self.worker_busy else 'idle'}"
            + (f" on job #{self.current_job_id}" if self.current_job_id else "")
            + ("" if self.worker_running else " (not running)"),
            f"Queue     : {self.queue_length} job(s) waiting or running",
        ]
        if self.vram_total_gb:
            lines.append(f"VRAM      : {self.vram_free_gb} / {self.vram_total_gb} GB free")
        lines.append(f"Workflows : {', '.join(self.workflows) or 'none'}")
        return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        *,
        users: UserService,
        jobs: JobRepository,
        registry: WorkflowRegistry,
        worker: GenerationWorker,
        comfy: ComfyUIClient,
        default_workflow: str,
    ) -> None:
        self._users = users
        self._jobs = jobs
        self._registry = registry
        self._worker = worker
        self._comfy = comfy
        self._default_workflow = default_workflow

    # ---------------- submission ----------------

    async def submit(self, request: GenerationRequest) -> Accepted:
        """Authorise, validate, persist, and queue. Raises on any refusal."""
        user = self._users.authorise(request.telegram_user_id)
        self._users.check_quota(user)

        workflow = self._resolve_workflow(request.workflow_id)
        text = self._clean_text(request.text)

        parameters = {"prompt": text, **request.parameters}
        # A seed baked into the template would make every generation identical, so the
        # same prompt could never produce a second variation. Roll one per job unless
        # the caller asked for a specific seed.
        if "seed" in workflow.user_parameters and "seed" not in parameters:
            parameters["seed"] = secrets.randbelow(2**32)
        # Validate now, so a bad request is refused at submit time with a useful
        # message rather than failing two minutes later in the worker.
        workflow.build(parameters, managed={"filename_prefix": "validation_probe"})

        job = await asyncio.to_thread(
            self._jobs.create,
            Job(
                telegram_user_id=request.telegram_user_id,
                telegram_chat_id=request.telegram_chat_id,
                telegram_message_id=request.telegram_message_id,
                original_request=text,
                workflow_id=workflow.workflow_id,
                parameters=parameters,
            ),
        )
        job = await asyncio.to_thread(self._jobs.transition, job.id, JobStatus.QUEUED)

        position = await asyncio.to_thread(self._jobs.queue_position, job.id)
        log.info(
            "orchestrator.accepted",
            job_id=job.id,
            user_id=user.telegram_user_id,
            workflow=workflow.workflow_id,
            queue_position=position,
        )
        return Accepted(job=job, queue_position=position, ahead_of_it=(position or 1) - 1)

    def _resolve_workflow(self, workflow_id: str | None) -> WorkflowSpec:
        return self._registry.get(workflow_id or self._default_workflow)

    @staticmethod
    def _clean_text(text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            raise ParameterError(
                "empty generation request",
                user_message="Please describe what you want generated.",
            )
        if len(cleaned) > MAX_REQUEST_LENGTH:
            raise ParameterError(
                f"request of {len(cleaned)} characters exceeds {MAX_REQUEST_LENGTH}",
                user_message=f"That request is too long (limit {MAX_REQUEST_LENGTH} characters).",
            )
        return cleaned

    # ---------------- queries ----------------

    async def job_for_user(self, job_id: int, telegram_user_id: int) -> Job | None:
        user = self._users.authorise(telegram_user_id)
        return await asyncio.to_thread(
            self._jobs.get_for_user, job_id, user.telegram_user_id
        )

    async def history(self, telegram_user_id: int, limit: int = 10) -> list[Job]:
        user = self._users.authorise(telegram_user_id)
        return await asyncio.to_thread(
            self._jobs.history_for_user, user.telegram_user_id, limit=limit
        )

    async def queue_view(self, telegram_user_id: int) -> list[Job]:
        """Admins see the whole queue; everyone else sees only their own jobs."""
        user = self._users.authorise(telegram_user_id)
        snapshot = await asyncio.to_thread(self._jobs.queue_snapshot)
        if user.is_admin:
            return snapshot
        return [job for job in snapshot if job.owned_by(user.telegram_user_id)]

    async def queue_position(self, job_id: int) -> int | None:
        return await asyncio.to_thread(self._jobs.queue_position, job_id)

    async def cancel(self, job_id: int, telegram_user_id: int) -> bool:
        """Cancel a job. Ownership-scoped unless the caller is an admin."""
        user = self._users.authorise(telegram_user_id)
        owner_scope = None if user.is_admin else user.telegram_user_id
        cancelled = await asyncio.to_thread(self._jobs.request_cancel, job_id, owner_scope)

        if cancelled:
            # Only interrupt ComfyUI if this exact job is the one on the GPU.
            if self._worker.current_job_id == job_id:
                try:
                    await self._comfy.interrupt()
                except Exception as exc:  # noqa: BLE001 - cancellation is best-effort
                    log.warning("orchestrator.interrupt_failed", job_id=job_id, error=str(exc))
        return cancelled

    async def system_status(self, telegram_user_id: int) -> SystemStatus:
        self._users.authorise(telegram_user_id)
        comfy = await self._comfy.status()
        active = await asyncio.to_thread(self._jobs.count_active)
        return SystemStatus(
            comfyui_online=comfy.online,
            comfyui_version=comfy.version,
            worker_running=self._worker.is_running,
            worker_busy=self._worker.is_busy,
            current_job_id=self._worker.current_job_id,
            queue_length=active,
            vram_free_gb=comfy.vram_free_gb,
            vram_total_gb=comfy.vram_total_gb,
            workflows=self._registry.ids(),
        )

    def quota_for(self, user: User):
        return self._users.quota_status(user)

    @property
    def registry(self) -> WorkflowRegistry:
        return self._registry

    @property
    def users(self) -> UserService:
        return self._users
