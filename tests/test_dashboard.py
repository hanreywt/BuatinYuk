"""The local dashboard: its API, and the boundary that keeps it local."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.database.connection import Database
from app.jobs.models import Job, JobStatus
from app.jobs.repository import JobRepository
from app.web.server import Dashboard, _kind_of


class FakeWorker:
    def __init__(self) -> None:
        self.is_running = True
        self.is_busy = False
        self.current_job_id = None
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False


class FakeComfy:
    def __init__(self) -> None:
        self.interrupted = 0

    async def status(self):
        from app.comfy.models import QueueState, SystemStatus

        return SystemStatus(online=True, version="0.33.2", devices=["cuda:0"],
                            vram_free_gb=2.0, vram_total_gb=10.0, queue=QueueState(0, 0))

    async def interrupt(self) -> None:
        self.interrupted += 1


@pytest.fixture
def repo(tmp_path: Path):
    db = Database(tmp_path / "d.db").connect()
    try:
        yield JobRepository(db)
    finally:
        db.close()


@pytest.fixture
def dashboard(repo):
    return Dashboard(jobs=repo, worker=FakeWorker(), comfy=FakeComfy())


def make_job(repo: JobRepository, **kw) -> Job:
    defaults = dict(telegram_user_id=1, telegram_chat_id=1,
                    original_request="a city", workflow_id="txt2img_h3_plate")
    return repo.create(Job(**{**defaults, **kw}))


# ---------------- the local-only boundary ----------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_dashboard_refuses_any_non_loopback_host(repo, host: str) -> None:
    """It exposes the queue and worker controls with no authentication."""
    with pytest.raises(ValueError, match="loopback"):
        Dashboard(jobs=repo, worker=FakeWorker(), comfy=FakeComfy(), host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_allowed(repo, host: str) -> None:
    assert Dashboard(jobs=repo, worker=FakeWorker(), comfy=FakeComfy(), host=host)


# ---------------- shaping ----------------


def test_job_view_carries_no_paths_or_internal_errors(repo, dashboard) -> None:
    job = make_job(repo)
    repo.transition(job.id, JobStatus.QUEUED)
    repo.transition(job.id, JobStatus.PREPARING)
    repo.transition(job.id, JobStatus.FAILED,
                    error_message=r"CUDA OOM at D:\models\x.safetensors",
                    user_message="Generation failed.")

    view = dashboard._describe(repo.get(job.id))
    serialised = repr(view)
    assert "D:\\" not in serialised
    assert "safetensors" not in serialised
    assert view["user_message"] == "Generation failed."


def test_job_view_reports_position_only_while_waiting(repo, dashboard) -> None:
    first = make_job(repo)
    second = make_job(repo)
    repo.transition(first.id, JobStatus.QUEUED)
    repo.transition(second.id, JobStatus.QUEUED)

    assert dashboard._describe(repo.get(second.id))["queue_position"] == 2
    repo.transition(first.id, JobStatus.PREPARING)
    assert dashboard._describe(repo.get(first.id))["queue_position"] is None


def test_long_requests_are_trimmed(repo, dashboard) -> None:
    job = make_job(repo, original_request="x" * 500)
    assert len(dashboard._describe(repo.get(job.id))["request"]) <= 120


@pytest.mark.parametrize(
    ("workflow", "kind"),
    [("txt2img_h3_plate", "image"), ("img2img_h3", "image"),
     ("txt2video_h3", "video"), ("img2video_h3", "video")],
)
def test_workflow_kind_is_derived_for_display(workflow: str, kind: str) -> None:
    assert _kind_of(workflow) == kind


# ---------------- statistics ----------------


def test_averages_ignore_failed_jobs(repo, dashboard) -> None:
    """A failure that died in two seconds would make the estimate a lie."""
    done = make_job(repo)
    for status in (JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.GENERATING,
                   JobStatus.COMPLETED):
        repo.transition(done.id, status)

    failed = make_job(repo)
    repo.transition(failed.id, JobStatus.QUEUED)
    repo.transition(failed.id, JobStatus.PREPARING)
    repo.transition(failed.id, JobStatus.FAILED)

    averages = repo.average_duration_by_workflow()
    assert [name for name, _, _ in averages] == ["txt2img_h3_plate"]
    assert [runs for _, _, runs in averages] == [1]


def test_recent_spans_all_users(repo) -> None:
    """Operator view, unlike the per-user history the bot serves."""
    make_job(repo, telegram_user_id=1)
    make_job(repo, telegram_user_id=2)
    assert len(repo.recent()) == 2


def test_recent_is_newest_first(repo) -> None:
    ids = [make_job(repo).id for _ in range(3)]
    assert [j.id for j in repo.recent()] == list(reversed(ids))


# ---------------- worker control ----------------


async def test_pause_and_resume_flip_the_worker(dashboard) -> None:
    await dashboard._pause(None)
    assert dashboard._worker.is_paused is True
    await dashboard._resume(None)
    assert dashboard._worker.is_paused is False


def test_pausing_does_not_stop_a_running_job() -> None:
    """Pause means "take nothing new"; stopping a running job is what cancel is for."""
    worker = FakeWorker()
    worker.is_busy = True
    worker.current_job_id = 7
    worker.pause()
    assert worker.is_paused and worker.is_busy and worker.current_job_id == 7
