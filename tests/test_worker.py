"""Queue worker and startup recovery, driven with a fake ComfyUI client."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.comfy.errors import ComfyExecutionFailed, ComfyTimeout, ComfyUnavailable
from app.comfy.models import OutputKind, OutputRef, Progress, QueueState
from app.database.connection import Database
from app.jobs.models import Job, JobStatus
from app.jobs.repository import JobRepository
from app.orchestrator.recovery import RecoveryService
from app.orchestrator.worker import GenerationWorker
from app.workflows.registry import WorkflowRegistry

OWNER = 4242


class FakeComfy:
    """Stands in for ComfyUIClient with the same surface the worker uses."""

    def __init__(self, *, outputs=None, fail_with=None, submit_error=None) -> None:
        self.outputs = outputs if outputs is not None else [OutputRef("a.png", "", "output")]
        self.fail_with = fail_with
        self.submit_error = submit_error
        self.submitted: list[dict] = []
        self.downloaded: list[OutputRef] = []
        self.history: dict[str, dict] = {}
        self.queue = QueueState(running=0, pending=0)
        self.progress_to_emit: list[Progress] = []

    async def submit(self, graph: dict) -> str:
        if self.submit_error:
            raise self.submit_error
        self.submitted.append(graph)
        return f"prompt-{len(self.submitted)}"

    async def wait(self, prompt_id, *, timeout, on_progress=None, cancelled=None):
        if on_progress:
            for progress in self.progress_to_emit:
                await on_progress(progress)
        if cancelled and cancelled():
            from app.comfy.errors import ComfyInterrupted

            raise ComfyInterrupted("cancelled")
        if self.fail_with:
            raise self.fail_with
        return self.outputs

    async def download(self, ref: OutputRef, destination: Path) -> Path:
        self.downloaded.append(ref)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-image-bytes")
        return destination

    async def queue_state(self) -> QueueState:
        return self.queue

    async def _history_entry(self, prompt_id: str):
        return self.history.get(prompt_id)


class RecordingNotifier:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.progress: list[tuple[int, str]] = []
        self.completed: list[tuple[int, list[Path]]] = []
        self.failed: list[tuple[int, str]] = []

    async def job_started(self, job: Job) -> None:
        self.started.append(job.id)

    async def job_progress(self, job: Job, message: str) -> None:
        self.progress.append((job.id, message))

    async def job_completed(self, job: Job, files: list[Path]) -> None:
        self.completed.append((job.id, files))

    async def job_failed(self, job: Job, user_message: str) -> None:
        self.failed.append((job.id, user_message))


@pytest.fixture
def repo(tmp_path: Path):
    database = Database(tmp_path / "worker.db").connect()
    try:
        yield JobRepository(database)
    finally:
        database.close()


@pytest.fixture
def registry(workflow_dir: Path) -> WorkflowRegistry:
    return WorkflowRegistry.load(workflow_dir, strict=True)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


def make_worker(repo, registry, comfy, tmp_path, notifier, **kwargs) -> GenerationWorker:
    return GenerationWorker(
        repository=repo,
        registry=registry,
        comfy=comfy,
        output_dir=tmp_path / "outputs",
        notifier=notifier,
        **kwargs,
    )


def queued_job(repo: JobRepository, **params) -> Job:
    job = repo.create(
        Job(
            telegram_user_id=OWNER,
            telegram_chat_id=OWNER,
            original_request="a city",
            workflow_id="test_wf",
            parameters={"prompt": "a city", **params},
        )
    )
    return repo.transition(job.id, JobStatus.QUEUED)


async def drain(worker: GenerationWorker, repo: JobRepository, job_id: int, timeout=5.0):
    """Run the worker until the job reaches a terminal state."""
    await worker.start()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if repo.get(job_id).status.is_terminal:
            break
        await asyncio.sleep(0.02)
    await worker.stop()
    return repo.get(job_id)


# ---------------- happy path ----------------


async def test_worker_runs_a_job_to_completion(repo, registry, tmp_path, notifier) -> None:
    comfy = FakeComfy(outputs=[OutputRef("a.png", "", "output"), OutputRef("b.png", "", "output")])
    job = queued_job(repo)

    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert finished.status is JobStatus.COMPLETED
    assert finished.comfy_prompt_id == "prompt-1"
    assert len(finished.outputs) == 2
    assert notifier.started == [job.id]
    assert notifier.completed and len(notifier.completed[0][1]) == 2
    assert not notifier.failed


async def test_outputs_land_under_the_owners_directory(repo, registry, tmp_path, notifier) -> None:
    comfy = FakeComfy()
    job = queued_job(repo)
    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    path = finished.outputs[0].path
    assert path.exists()
    assert f"user_{OWNER}" in path.parts
    assert path.name.startswith(f"job_{job.id:06d}_u{OWNER}")
    # Everything stays inside the application's own output directory.
    assert (tmp_path / "outputs").resolve() in path.resolve().parents


async def test_worker_records_the_values_actually_sent(repo, registry, tmp_path, notifier) -> None:
    """Out-of-range input is clamped, and the clamped value is what gets stored."""
    comfy = FakeComfy()
    job = queued_job(repo, width=99999)
    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert finished.parameters["width"] == 1024
    assert comfy.submitted[0]["1"]["inputs"]["width"] == 1024


async def test_managed_prefix_is_applied_to_the_graph(repo, registry, tmp_path, notifier) -> None:
    comfy = FakeComfy()
    job = queued_job(repo)
    await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert comfy.submitted[0]["3"]["inputs"]["filename_prefix"] == f"job_{job.id:06d}_u{OWNER}"


async def test_jobs_run_strictly_one_at_a_time(repo, registry, tmp_path, notifier) -> None:
    comfy = FakeComfy()
    jobs = [queued_job(repo) for _ in range(3)]
    worker = make_worker(repo, registry, comfy, tmp_path, notifier)

    await worker.start()
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if all(repo.get(j.id).status.is_terminal for j in jobs):
            break
        await asyncio.sleep(0.02)
    await worker.stop()

    assert all(repo.get(j.id).status is JobStatus.COMPLETED for j in jobs)
    # FIFO order preserved.
    assert notifier.started == [j.id for j in jobs]


# ---------------- failures ----------------


async def test_execution_failure_is_recorded_with_a_safe_message(
    repo, registry, tmp_path, notifier
) -> None:
    comfy = FakeComfy(fail_with=ComfyExecutionFailed("CUDA OOM at D:\\models\\x.safetensors"))
    job = queued_job(repo)
    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert finished.status is JobStatus.FAILED
    assert "D:\\models" in finished.error_message
    assert "D:\\" not in finished.user_message
    assert notifier.failed[0][0] == job.id
    assert "D:\\" not in notifier.failed[0][1]


async def test_comfyui_offline_fails_the_job_cleanly(repo, registry, tmp_path, notifier) -> None:
    comfy = FakeComfy(submit_error=ComfyUnavailable("connection refused"))
    job = queued_job(repo)
    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert finished.status is JobStatus.FAILED
    assert "offline" in finished.user_message.lower()


async def test_timeout_fails_the_job(repo, registry, tmp_path, notifier) -> None:
    comfy = FakeComfy(fail_with=ComfyTimeout("too slow"))
    job = queued_job(repo)
    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert finished.status is JobStatus.FAILED
    assert "too long" in finished.user_message.lower()


async def test_invalid_parameters_fail_before_reaching_comfyui(
    repo, registry, tmp_path, notifier
) -> None:
    comfy = FakeComfy()
    job = repo.create(
        Job(
            telegram_user_id=OWNER,
            telegram_chat_id=OWNER,
            original_request="x",
            workflow_id="test_wf",
            parameters={"prompt": "a" * 500},  # exceeds max_length of 100
        )
    )
    repo.transition(job.id, JobStatus.QUEUED)
    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert finished.status is JobStatus.FAILED
    assert comfy.submitted == []  # the GPU was never asked to do anything


async def test_unknown_workflow_fails_the_job(repo, registry, tmp_path, notifier) -> None:
    comfy = FakeComfy()
    job = repo.create(
        Job(
            telegram_user_id=OWNER,
            telegram_chat_id=OWNER,
            original_request="x",
            workflow_id="no_such_workflow",
            parameters={"prompt": "x"},
        )
    )
    repo.transition(job.id, JobStatus.QUEUED)
    finished = await drain(make_worker(repo, registry, comfy, tmp_path, notifier), repo, job.id)

    assert finished.status is JobStatus.FAILED
    assert comfy.submitted == []


async def test_a_failing_notifier_does_not_stop_the_queue(repo, registry, tmp_path) -> None:
    class BrokenNotifier(RecordingNotifier):
        async def job_started(self, job: Job) -> None:
            raise RuntimeError("telegram is down")

    comfy = FakeComfy()
    broken = BrokenNotifier()
    jobs = [queued_job(repo) for _ in range(2)]
    worker = make_worker(repo, registry, comfy, tmp_path, broken)

    await worker.start()
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if all(repo.get(j.id).status.is_terminal for j in jobs):
            break
        await asyncio.sleep(0.02)
    await worker.stop()

    assert all(repo.get(j.id).status is JobStatus.COMPLETED for j in jobs)


# ---------------- cancellation ----------------


async def test_cancelling_before_submission_skips_the_gpu(
    repo, registry, tmp_path, notifier
) -> None:
    comfy = FakeComfy()
    job = queued_job(repo)
    repo.request_cancel(job.id, OWNER)

    worker = make_worker(repo, registry, comfy, tmp_path, notifier)
    await worker.start()
    await asyncio.sleep(0.3)
    await worker.stop()

    # A cancelled job is never claimed by the worker, so it stays queued and unsent.
    assert comfy.submitted == []


async def test_cancel_during_generation_marks_the_job_cancelled(
    repo, registry, tmp_path, notifier
) -> None:
    class CancellingRepo(JobRepository):
        def is_cancel_requested(self, job_id: int) -> bool:
            return True

    comfy = FakeComfy()
    job = queued_job(repo)
    cancelling = CancellingRepo(repo._db)
    finished = await drain(
        make_worker(cancelling, registry, comfy, tmp_path, notifier), repo, job.id
    )

    assert finished.status is JobStatus.CANCELLED


# ---------------- lifecycle ----------------


async def test_worker_start_is_idempotent(repo, registry, tmp_path, notifier) -> None:
    worker = make_worker(repo, registry, FakeComfy(), tmp_path, notifier)
    await worker.start()
    await worker.start()
    assert worker.is_running
    await worker.stop()
    assert not worker.is_running


async def test_idle_worker_reports_not_busy(repo, registry, tmp_path, notifier) -> None:
    worker = make_worker(repo, registry, FakeComfy(), tmp_path, notifier)
    await worker.start()
    await asyncio.sleep(0.05)
    assert worker.is_busy is False
    assert worker.current_job_id is None
    await worker.stop()


# ---------------- recovery ----------------


def generating_job(repo: JobRepository, prompt_id: str | None = "p-1") -> Job:
    job = queued_job(repo)
    repo.transition(job.id, JobStatus.PREPARING)
    return repo.transition(job.id, JobStatus.GENERATING, comfy_prompt_id=prompt_id)


async def test_recovery_requeues_work_that_was_never_submitted(repo, tmp_path) -> None:
    received = repo.create(
        Job(telegram_user_id=OWNER, telegram_chat_id=OWNER,
            original_request="x", workflow_id="test_wf")
    )
    preparing = queued_job(repo)
    repo.transition(preparing.id, JobStatus.PREPARING)

    service = RecoveryService(repository=repo, comfy=FakeComfy(), output_dir=tmp_path)
    report = await service.reconcile()

    assert set(report.requeued) == {received.id, preparing.id}
    assert repo.get(received.id).status is JobStatus.QUEUED
    assert repo.get(preparing.id).status is JobStatus.QUEUED


async def test_recovery_keeps_a_generation_that_finished_while_we_were_down(
    repo, tmp_path
) -> None:
    """The expensive result must not be thrown away and repeated."""
    job = generating_job(repo)
    comfy = FakeComfy()
    comfy.history["p-1"] = {
        "status": {"status_str": "success", "completed": True},
        "outputs": {"3": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}},
    }

    service = RecoveryService(repository=repo, comfy=comfy, output_dir=tmp_path)
    report = await service.reconcile()

    assert report.recovered == [job.id]
    recovered = repo.get(job.id)
    assert recovered.status is JobStatus.COMPLETED
    assert len(recovered.outputs) == 1
    assert recovered.outputs[0].path.exists()


async def test_recovery_fails_a_generation_comfyui_errored(repo, tmp_path) -> None:
    job = generating_job(repo)
    comfy = FakeComfy()
    comfy.history["p-1"] = {"status": {"status_str": "error", "completed": False}, "outputs": {}}

    report = await RecoveryService(
        repository=repo, comfy=comfy, output_dir=tmp_path
    ).reconcile()

    assert report.failed == [job.id]
    assert repo.get(job.id).status is JobStatus.FAILED


async def test_recovery_does_not_duplicate_an_unknown_generation(repo, tmp_path) -> None:
    """No history and an empty queue means it is gone - fail it rather than repeat it."""
    job = generating_job(repo)
    comfy = FakeComfy()  # empty history, empty queue

    report = await RecoveryService(
        repository=repo, comfy=comfy, output_dir=tmp_path
    ).reconcile()

    assert report.failed == [job.id]
    assert repo.get(job.id).status is JobStatus.FAILED
    assert repo.get(job.id).status is not JobStatus.QUEUED  # never silently repeated


async def test_recovery_leaves_a_job_alone_while_comfyui_is_still_busy(repo, tmp_path) -> None:
    job = generating_job(repo)
    comfy = FakeComfy()
    comfy.queue = QueueState(running=1, pending=0)

    report = await RecoveryService(
        repository=repo, comfy=comfy, output_dir=tmp_path
    ).reconcile()

    assert report.still_running == [job.id]
    assert repo.get(job.id).status is JobStatus.GENERATING


async def test_recovery_leaves_jobs_alone_when_comfyui_is_unreachable(repo, tmp_path) -> None:
    job = generating_job(repo)

    class DeadComfy(FakeComfy):
        async def _history_entry(self, prompt_id: str):
            raise ComfyUnavailable("down")

    report = await RecoveryService(
        repository=repo, comfy=DeadComfy(), output_dir=tmp_path
    ).reconcile()

    assert report.still_running == [job.id]
    assert repo.get(job.id).status is JobStatus.GENERATING


async def test_recovery_fails_a_generating_job_with_no_prompt_id(repo, tmp_path) -> None:
    job = generating_job(repo, prompt_id=None)

    report = await RecoveryService(
        repository=repo, comfy=FakeComfy(), output_dir=tmp_path
    ).reconcile()

    assert report.failed == [job.id]
    assert "restart" in repo.get(job.id).user_message.lower()


async def test_recovery_on_a_clean_queue_does_nothing(repo, tmp_path) -> None:
    report = await RecoveryService(
        repository=repo, comfy=FakeComfy(), output_dir=tmp_path
    ).reconcile()

    assert report.total == 0
    assert report.summary() == "no interrupted jobs"


async def test_queued_jobs_survive_recovery_untouched(repo, tmp_path) -> None:
    job = queued_job(repo)
    report = await RecoveryService(
        repository=repo, comfy=FakeComfy(), output_dir=tmp_path
    ).reconcile()

    assert report.total == 0
    assert repo.get(job.id).status is JobStatus.QUEUED
