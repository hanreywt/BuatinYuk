"""The single GPU worker.

One worker, one job at a time. The machine has one 10 GB card running a model that
already offloads heavily; running two generations at once would thrash rather than
parallelise. Sequential processing is the deliberate default, not a simplification.

The worker owns the whole lifecycle of a job once it is queued: building the graph,
submitting it, watching it, downloading the results, and recording the outcome. It
reports progress through a `JobNotifier` and never imports Telegram.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.comfy.client import ComfyUIClient
from app.comfy.errors import ComfyError, ComfyInterrupted, ComfyUnavailable
from app.comfy.models import OutputRef, Progress
from app.jobs.models import InvalidTransition, Job, JobOutput, JobStatus
from app.jobs.repository import JobRepository
from app.orchestrator.notifier import JobNotifier, NullNotifier
from app.utils.logging import get_logger
from app.utils.paths import safe_join
from app.workflows.registry import WorkflowError, WorkflowRegistry

log = get_logger(__name__)

#: How often to look for newly queued work when the queue is empty.
IDLE_POLL_SECONDS = 2.0
#: How long to wait before retrying the loop after an unexpected error.
ERROR_BACKOFF_SECONDS = 5.0
#: Progress is reported to the user at most this often, to avoid flooding a chat.
PROGRESS_INTERVAL_SECONDS = 20.0

GENERIC_FAILURE = "Generation failed. Please try again."


class GenerationWorker:
    """Drains the job queue, one job at a time."""

    def __init__(
        self,
        *,
        repository: JobRepository,
        registry: WorkflowRegistry,
        comfy: ComfyUIClient,
        output_dir: Path,
        notifier: JobNotifier | None = None,
        job_timeout: float = 1800.0,
    ) -> None:
        self._repo = repository
        self._registry = registry
        self._comfy = comfy
        self._output_dir = output_dir
        self._notifier = notifier or NullNotifier()
        self._job_timeout = job_timeout

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._current_job_id: int | None = None

    # ---------------- lifecycle ----------------

    @property
    def current_job_id(self) -> int | None:
        return self._current_job_id

    @property
    def is_busy(self) -> bool:
        return self._current_job_id is not None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="generation-worker")
        log.info("worker.started")

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Ask the worker to finish its current step and exit.

        A running generation is left with ComfyUI rather than interrupted; startup
        recovery reconciles it next time.
        """
        self._stopping.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except TimeoutError:
            log.warning("worker.stop.timeout", job_id=self._current_job_id)
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        finally:
            self._task = None
            log.info("worker.stopped")

    # ---------------- main loop ----------------

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                job = await asyncio.to_thread(self._repo.next_queued)
                if job is None:
                    await self._sleep(IDLE_POLL_SECONDS)
                    continue
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any one job
                log.exception("worker.loop.error", error=str(exc))
                await self._sleep(ERROR_BACKOFF_SECONDS)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately if a stop was requested."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return

    # ---------------- one job ----------------

    async def _process(self, job: Job) -> None:
        assert job.id is not None  # noqa: S101 - persisted jobs always have an id
        self._current_job_id = job.id
        try:
            await self._run_job(job)
        finally:
            self._current_job_id = None

    async def _run_job(self, job: Job) -> None:
        job_id = job.id
        assert job_id is not None  # noqa: S101

        # Claim the job. If a cancel or another actor got here first, move on quietly.
        try:
            job = await asyncio.to_thread(
                self._repo.transition, job_id, JobStatus.PREPARING, expect=JobStatus.QUEUED
            )
        except InvalidTransition:
            log.info("worker.job.claim_lost", job_id=job_id)
            return

        log.info("worker.job.start", job_id=job_id, workflow=job.workflow_id)
        await self._safe_notify(self._notifier.job_started(job))

        try:
            graph = await self._build_graph(job)
            prompt_id = await self._submit(job, graph)
            outputs = await self._await_outputs(job, prompt_id)
            files = await self._store_outputs(job, outputs)
        except ComfyInterrupted:
            await self._finish_cancelled(job)
            return
        except (ComfyError, WorkflowError) as exc:
            await self._finish_failed(job, exc, exc.user_message)
            return
        except Exception as exc:  # noqa: BLE001 - never let one job kill the worker
            log.exception("worker.job.unexpected", job_id=job_id, error=str(exc))
            await self._finish_failed(job, exc, GENERIC_FAILURE)
            return

        job = await asyncio.to_thread(
            self._repo.transition, job_id, JobStatus.COMPLETED, expect=JobStatus.GENERATING
        )
        log.info(
            "worker.job.completed",
            job_id=job_id,
            outputs=len(files),
            seconds=round(job.duration_seconds or 0),
        )
        await self._safe_notify(self._notifier.job_completed(job, files))

    # ---------------- steps ----------------

    async def _build_graph(self, job: Job) -> dict:
        workflow = self._registry.get(job.workflow_id)

        # A stored job mixes user settings with orchestrator-managed ones such as the
        # uploaded image reference. Split them back apart, because build() refuses a
        # managed name arriving as an ordinary parameter - which is exactly the check
        # that stops a request from setting one.
        managed: dict[str, object] = {"filename_prefix": job.output_prefix()}
        user_params: dict[str, object] = {}
        for name, value in job.parameters.items():
            spec = workflow.parameters.get(name)
            if spec is not None and spec.managed:
                managed[name] = value
            else:
                user_params[name] = value

        graph = workflow.build(user_params, managed=managed)
        # Record exactly what was sent, after clamping and snapping.
        applied = {
            name: graph[spec.targets[0][0]]["inputs"][spec.targets[0][1]]
            for name, spec in workflow.user_parameters.items()
            if name in job.parameters
        }
        # Keep managed values (such as the uploaded image) in the record too, so the
        # job stays a complete account of what ran.
        applied.update({name: value for name, value in managed.items()
                        if name != "filename_prefix"})
        await asyncio.to_thread(self._repo.set_parameters, job.id, applied)
        job.parameters = applied
        return graph

    async def _submit(self, job: Job, graph: dict) -> str:
        if await asyncio.to_thread(self._repo.is_cancel_requested, job.id):
            raise ComfyInterrupted(f"job {job.id} cancelled before submission")

        prompt_id = await self._comfy.submit(graph)
        await asyncio.to_thread(
            self._repo.transition,
            job.id,
            JobStatus.GENERATING,
            comfy_prompt_id=prompt_id,
            expect=JobStatus.PREPARING,
        )
        return prompt_id

    async def _await_outputs(self, job: Job, prompt_id: str) -> list[OutputRef]:
        last_report = 0.0
        loop = asyncio.get_running_loop()

        async def on_progress(progress: Progress) -> None:
            nonlocal last_report
            now = loop.time()
            if now - last_report < PROGRESS_INTERVAL_SECONDS:
                return
            text = _describe(progress)
            if text is None:
                return
            last_report = now
            await self._safe_notify(self._notifier.job_progress(job, text))

        def cancelled() -> bool:
            # Called from the polling loop; a blocking read here is acceptable and
            # keeps cancellation checks on the same cadence as status polling.
            return self._repo.is_cancel_requested(job.id)

        return await self._comfy.wait(
            prompt_id,
            timeout=self._job_timeout,
            on_progress=on_progress,
            cancelled=cancelled,
        )

    async def _store_outputs(self, job: Job, refs: list[OutputRef]) -> list[Path]:
        """Copy results into this application's output directory, one folder per job.

        ComfyUI supplies the filenames, so every one goes through `safe_join`; the
        stored name is derived from the job id rather than from anything ComfyUI said.
        """
        job_dir = safe_join(self._output_dir, f"user_{job.telegram_user_id}", job.output_prefix())
        stored: list[Path] = []

        for index, ref in enumerate(refs, start=1):
            suffix = Path(ref.filename).suffix.lower() or ".bin"
            destination = safe_join(job_dir, f"{job.output_prefix()}_{index:03d}{suffix}")
            path = await self._comfy.download(ref, destination)
            stored.append(path)
            await asyncio.to_thread(
                self._repo.add_output,
                JobOutput(
                    job_id=job.id,
                    path=path,
                    kind=ref.actual_kind.name.lower(),
                    size_bytes=path.stat().st_size,
                ),
            )
        return stored

    # ---------------- endings ----------------

    async def _finish_failed(self, job: Job, exc: Exception, user_message: str) -> None:
        log.warning(
            "worker.job.failed",
            job_id=job.id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        try:
            job = await asyncio.to_thread(
                self._repo.transition,
                job.id,
                JobStatus.FAILED,
                error_message=f"{type(exc).__name__}: {exc}",
                user_message=user_message,
            )
        except (InvalidTransition, LookupError):
            log.warning("worker.job.already_terminal", job_id=job.id)
            return
        await self._safe_notify(self._notifier.job_failed(job, user_message))

    async def _finish_cancelled(self, job: Job) -> None:
        try:
            job = await asyncio.to_thread(
                self._repo.transition,
                job.id,
                JobStatus.CANCELLED,
                user_message="Cancelled.",
            )
        except (InvalidTransition, LookupError):
            return
        log.info("worker.job.cancelled", job_id=job.id)
        await self._safe_notify(self._notifier.job_failed(job, "Cancelled."))

    async def _safe_notify(self, awaitable) -> None:
        """A broken notifier must never stop the queue."""
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001
            log.warning("worker.notify.failed", error=str(exc))


def _describe(progress: Progress) -> str | None:
    if progress.percent is not None:
        return f"Generating… {progress.percent}%"
    if progress.queue_remaining:
        return f"Waiting for the generator… {progress.queue_remaining} ahead"
    return None
