"""Startup reconciliation.

A restart must not corrupt the queue, and it must not silently repeat expensive work.
Each interrupted job is resolved by what is actually known about it:

| Left in     | Was it submitted? | Action |
|-------------|-------------------|--------|
| RECEIVED    | no                | queue it - nothing was spent |
| QUEUED      | no                | leave it - the worker will pick it up |
| PREPARING   | no                | requeue - the graph was never sent |
| GENERATING  | yes               | ask ComfyUI what happened, and only then decide |

A GENERATING job is never blindly requeued: ComfyUI may have finished it, may still be
running it, or may have lost it in its own restart. Guessing risks either duplicating
two minutes of GPU time or throwing away a finished result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from app.comfy.client import ComfyUIClient
from app.comfy.errors import ComfyError
from app.jobs.models import InvalidTransition, Job, JobOutput, JobStatus
from app.jobs.repository import JobRepository
from app.utils.logging import get_logger
from app.utils.paths import safe_join

log = get_logger(__name__)

RECOVERED_INCOMPLETE = (
    "This job was interrupted when the server restarted and could not be recovered. "
    "Please send it again."
)


@dataclass(slots=True)
class RecoveryReport:
    """What startup did, so it can be logged and shown to an admin."""

    requeued: list[int] = field(default_factory=list)
    recovered: list[int] = field(default_factory=list)
    still_running: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.recovered) + len(self.still_running) + len(self.failed)

    def summary(self) -> str:
        if self.total == 0:
            return "no interrupted jobs"
        parts = []
        if self.requeued:
            parts.append(f"{len(self.requeued)} requeued")
        if self.recovered:
            parts.append(f"{len(self.recovered)} recovered as completed")
        if self.still_running:
            parts.append(f"{len(self.still_running)} still running in ComfyUI")
        if self.failed:
            parts.append(f"{len(self.failed)} marked failed")
        return ", ".join(parts)


class RecoveryService:
    def __init__(
        self,
        *,
        repository: JobRepository,
        comfy: ComfyUIClient,
        output_dir: Path,
    ) -> None:
        self._repo = repository
        self._comfy = comfy
        self._output_dir = output_dir

    async def reconcile(self) -> RecoveryReport:
        report = RecoveryReport()
        interrupted = await asyncio.to_thread(self._repo.interrupted_jobs)
        if not interrupted:
            log.info("recovery.clean")
            return report

        log.info("recovery.start", count=len(interrupted))
        for job in interrupted:
            try:
                await self._reconcile_one(job, report)
            except Exception as exc:  # noqa: BLE001 - one bad job must not block startup
                log.exception("recovery.job.error", job_id=job.id, error=str(exc))
                report.failed.append(job.id)

        log.info("recovery.done", summary=report.summary())
        return report

    async def _reconcile_one(self, job: Job, report: RecoveryReport) -> None:
        if job.status is JobStatus.QUEUED:
            return  # already in the right place

        if job.status in (JobStatus.RECEIVED, JobStatus.PREPARING):
            # Nothing was ever sent to ComfyUI, so requeueing cannot duplicate work.
            await self._requeue(job, report)
            return

        if job.status is JobStatus.GENERATING:
            await self._reconcile_generating(job, report)

    async def _requeue(self, job: Job, report: RecoveryReport) -> None:
        try:
            await asyncio.to_thread(self._repo.transition, job.id, JobStatus.QUEUED)
        except InvalidTransition as exc:
            # Silence here once hid a missing transition; make it visible instead.
            log.warning("recovery.requeue_refused", job_id=job.id, error=str(exc))
            report.failed.append(job.id)
            return
        report.requeued.append(job.id)
        log.info("recovery.requeued", job_id=job.id, was=job.status.value)

    async def _reconcile_generating(self, job: Job, report: RecoveryReport) -> None:
        """Ask ComfyUI what became of a job that was running when we stopped."""
        if not job.comfy_prompt_id:
            # Submitted state was never recorded, so we cannot tell whether it ran.
            # Failing is the safe choice: a duplicate costs GPU time and confuses the user.
            await self._fail(job, report, "no ComfyUI prompt id was recorded")
            return

        try:
            entry = await self._comfy._history_entry(job.comfy_prompt_id)
        except ComfyError as exc:
            # ComfyUI is not answering. Leave the job alone rather than guess; the next
            # startup, or an admin, can resolve it once ComfyUI is back.
            log.warning("recovery.comfy_unavailable", job_id=job.id, error=str(exc))
            report.still_running.append(job.id)
            return

        if entry is None:
            queue_state = await self._comfy.queue_state()
            if queue_state.total > 0:
                # Something is running and it may well be this job; do not disturb it.
                log.info("recovery.left_running", job_id=job.id)
                report.still_running.append(job.id)
                return
            await self._fail(job, report, "ComfyUI has no record of this prompt")
            return

        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            await self._fail(job, report, "ComfyUI reported an execution error")
            return

        if status.get("completed") is True or status.get("status_str") == "success":
            await self._recover_outputs(job, entry, report)
            return

        report.still_running.append(job.id)

    async def _recover_outputs(self, job: Job, entry: dict, report: RecoveryReport) -> None:
        """The generation actually finished while we were down - keep the results."""
        from app.comfy.client import _extract_outputs

        try:
            refs = _extract_outputs(entry)
        except ComfyError:
            await self._fail(job, report, "completed in ComfyUI but produced no output")
            return

        job_dir = safe_join(
            self._output_dir, f"user_{job.telegram_user_id}", job.output_prefix()
        )
        stored = 0
        for index, ref in enumerate(refs, start=1):
            suffix = Path(ref.filename).suffix.lower() or ".bin"
            destination = safe_join(job_dir, f"{job.output_prefix()}_{index:03d}{suffix}")
            try:
                path = await self._comfy.download(ref, destination)
            except ComfyError as exc:
                log.warning("recovery.download_failed", job_id=job.id, error=str(exc))
                continue
            await asyncio.to_thread(
                self._repo.add_output,
                JobOutput(
                    job_id=job.id,
                    path=path,
                    kind=ref.actual_kind.name.lower(),
                    size_bytes=path.stat().st_size,
                ),
            )
            stored += 1

        if stored == 0:
            await self._fail(job, report, "outputs could no longer be retrieved")
            return

        try:
            await asyncio.to_thread(self._repo.transition, job.id, JobStatus.COMPLETED)
        except InvalidTransition:
            return
        report.recovered.append(job.id)
        log.info("recovery.completed", job_id=job.id, outputs=stored)

    async def _fail(self, job: Job, report: RecoveryReport, reason: str) -> None:
        try:
            await asyncio.to_thread(
                self._repo.transition,
                job.id,
                JobStatus.FAILED,
                error_message=f"unrecoverable after restart: {reason}",
                user_message=RECOVERED_INCOMPLETE,
            )
        except (InvalidTransition, LookupError):
            return
        report.failed.append(job.id)
        log.info("recovery.failed", job_id=job.id, reason=reason)
