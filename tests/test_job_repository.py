"""Job persistence, the state machine, ownership scoping, and queue behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database.connection import Database
from app.jobs.models import InvalidTransition, Job, JobOutput, JobStatus, can_transition
from app.jobs.repository import JobRepository

OWNER = 1001
OTHER = 2002


@pytest.fixture
def repo(tmp_path: Path):
    database = Database(tmp_path / "test.db").connect()
    try:
        yield JobRepository(database)
    finally:
        database.close()


def make_job(user_id: int = OWNER, **kwargs) -> Job:
    defaults = {
        "telegram_user_id": user_id,
        "telegram_chat_id": user_id,
        "original_request": "a city at night",
        "workflow_id": "txt2img_h3_plate",
    }
    return Job(**{**defaults, **kwargs})


def queue(repo: JobRepository, job: Job) -> Job:
    return repo.transition(job.id, JobStatus.QUEUED)


# ---------------- persistence ----------------


def test_created_job_round_trips(repo: JobRepository) -> None:
    created = repo.create(make_job(parameters={"prompt": "x", "steps": 12}))
    assert created.id is not None

    loaded = repo.get(created.id)
    assert loaded.telegram_user_id == OWNER
    assert loaded.workflow_id == "txt2img_h3_plate"
    assert loaded.parameters == {"prompt": "x", "steps": 12}
    assert loaded.status is JobStatus.RECEIVED
    assert loaded.created_at.tzinfo is not None


def test_job_survives_a_reconnect(tmp_path: Path) -> None:
    """State must outlive the process, not just the object graph."""
    path = tmp_path / "persist.db"
    database = Database(path).connect()
    job_id = JobRepository(database).create(make_job()).id
    database.close()

    reopened = Database(path).connect()
    try:
        recovered = JobRepository(reopened).get(job_id)
        assert recovered is not None and recovered.status is JobStatus.RECEIVED
    finally:
        reopened.close()


def test_ids_increase_so_fifo_order_is_stable(repo: JobRepository) -> None:
    ids = [repo.create(make_job()).id for _ in range(3)]
    assert ids == sorted(ids)


# ---------------- state machine ----------------


def test_happy_path_transitions(repo: JobRepository) -> None:
    job = repo.create(make_job())
    for status in (
        JobStatus.QUEUED,
        JobStatus.PREPARING,
        JobStatus.GENERATING,
        JobStatus.COMPLETED,
    ):
        job = repo.transition(job.id, status)
    assert job.status is JobStatus.COMPLETED
    assert job.is_terminal


def test_started_at_is_set_once_work_begins(repo: JobRepository) -> None:
    job = repo.create(make_job())
    assert job.started_at is None
    queue(repo, job)
    job = repo.transition(job.id, JobStatus.PREPARING)
    assert job.started_at is not None
    assert job.finished_at is None


def test_finished_at_is_set_on_every_terminal_state(repo: JobRepository) -> None:
    for terminal in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        job = repo.create(make_job())
        queue(repo, job)
        repo.transition(job.id, JobStatus.PREPARING)
        if terminal is JobStatus.COMPLETED:
            repo.transition(job.id, JobStatus.GENERATING)
        job = repo.transition(job.id, terminal)
        assert job.finished_at is not None, terminal


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (JobStatus.RECEIVED, JobStatus.GENERATING),
        (JobStatus.RECEIVED, JobStatus.COMPLETED),
        (JobStatus.QUEUED, JobStatus.COMPLETED),
        (JobStatus.COMPLETED, JobStatus.QUEUED),
        (JobStatus.FAILED, JobStatus.GENERATING),
        (JobStatus.CANCELLED, JobStatus.QUEUED),
    ],
)
def test_illegal_transitions_are_refused(
    repo: JobRepository, start: JobStatus, target: JobStatus
) -> None:
    job = repo.create(make_job())
    # Walk to `start` legally first.
    path = {
        JobStatus.RECEIVED: [],
        JobStatus.QUEUED: [JobStatus.QUEUED],
        JobStatus.COMPLETED: [
            JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.GENERATING, JobStatus.COMPLETED,
        ],
        JobStatus.FAILED: [JobStatus.QUEUED, JobStatus.FAILED],
        JobStatus.CANCELLED: [JobStatus.CANCELLED],
    }[start]
    for step in path:
        repo.transition(job.id, step)

    with pytest.raises(InvalidTransition):
        repo.transition(job.id, target)
    assert repo.get(job.id).status is start


def test_terminal_states_are_final(repo: JobRepository) -> None:
    assert JobStatus.COMPLETED.is_terminal and not JobStatus.COMPLETED.is_active
    assert JobStatus.QUEUED.is_active
    assert not can_transition(JobStatus.COMPLETED, JobStatus.QUEUED)


def test_expect_guards_against_a_concurrent_change(repo: JobRepository) -> None:
    """Two actors racing on one job: the stale one must lose."""
    job = repo.create(make_job())
    queue(repo, job)
    repo.transition(job.id, JobStatus.CANCELLED)

    with pytest.raises(InvalidTransition):
        repo.transition(job.id, JobStatus.PREPARING, expect=JobStatus.QUEUED)


def test_transition_records_prompt_id_and_messages(repo: JobRepository) -> None:
    job = repo.create(make_job())
    queue(repo, job)
    repo.transition(job.id, JobStatus.PREPARING)
    repo.transition(job.id, JobStatus.GENERATING, comfy_prompt_id="p-42")
    job = repo.transition(
        job.id,
        JobStatus.FAILED,
        error_message="CUDA OOM at D:\\models\\x.safetensors",
        user_message="Generation failed. Please try again.",
    )
    assert job.comfy_prompt_id == "p-42"
    assert "D:\\models" in job.error_message  # detail is kept for the operator
    assert "D:\\" not in job.user_message  # but not in what the user sees


def test_transition_on_a_missing_job_raises(repo: JobRepository) -> None:
    with pytest.raises(LookupError):
        repo.transition(9999, JobStatus.QUEUED)


# ---------------- ownership ----------------


def test_a_user_cannot_read_another_users_job(repo: JobRepository) -> None:
    job = repo.create(make_job(OWNER))
    assert repo.get_for_user(job.id, OWNER) is not None
    assert repo.get_for_user(job.id, OTHER) is None


def test_history_is_scoped_to_the_asking_user(repo: JobRepository) -> None:
    for _ in range(3):
        repo.create(make_job(OWNER))
    repo.create(make_job(OTHER))

    assert len(repo.history_for_user(OWNER)) == 3
    assert len(repo.history_for_user(OTHER)) == 1
    assert all(j.telegram_user_id == OWNER for j in repo.history_for_user(OWNER))


def test_history_is_newest_first_and_limited(repo: JobRepository) -> None:
    ids = [repo.create(make_job(OWNER)).id for _ in range(5)]
    history = repo.history_for_user(OWNER, limit=2)
    assert [j.id for j in history] == [ids[-1], ids[-2]]


def test_latest_completed_never_crosses_users(repo: JobRepository) -> None:
    """Backs "upscale that" - it must not reach another user's image."""
    theirs = repo.create(make_job(OTHER))
    for status in (JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.GENERATING,
                   JobStatus.COMPLETED):
        repo.transition(theirs.id, status)

    assert repo.latest_completed_for_user(OTHER).id == theirs.id
    assert repo.latest_completed_for_user(OWNER) is None


def test_outputs_are_scoped_to_the_owner(repo: JobRepository, tmp_path: Path) -> None:
    job = repo.create(make_job(OWNER))
    repo.add_output(JobOutput(job_id=job.id, path=tmp_path / "a.png", size_bytes=10))

    assert len(repo.outputs_for_user_job(job.id, OWNER)) == 1
    assert repo.outputs_for_user_job(job.id, OTHER) == []


def test_outputs_are_deleted_with_their_job(repo: JobRepository, tmp_path: Path) -> None:
    job = repo.create(make_job())
    repo.add_output(JobOutput(job_id=job.id, path=tmp_path / "a.png"))
    with repo._db.transaction() as connection:
        connection.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
    assert repo.outputs_for_job(job.id) == []


def test_a_user_cannot_cancel_another_users_job(repo: JobRepository) -> None:
    job = repo.create(make_job(OWNER))
    queue(repo, job)
    assert repo.request_cancel(job.id, OTHER) is False
    assert repo.is_cancel_requested(job.id) is False
    assert repo.request_cancel(job.id, OWNER) is True


# ---------------- queue ----------------


def test_next_queued_is_strict_fifo(repo: JobRepository) -> None:
    jobs = [repo.create(make_job()) for _ in range(3)]
    for job in jobs:
        queue(repo, job)
    assert repo.next_queued().id == jobs[0].id


def test_next_queued_ignores_unqueued_and_cancelled_jobs(repo: JobRepository) -> None:
    first = repo.create(make_job())   # left in RECEIVED
    second = repo.create(make_job())
    queue(repo, second)
    repo.request_cancel(second.id)
    third = repo.create(make_job())
    queue(repo, third)

    assert repo.next_queued().id == third.id
    assert first.status is JobStatus.RECEIVED


def test_queue_position_counts_only_jobs_ahead(repo: JobRepository) -> None:
    jobs = [repo.create(make_job()) for _ in range(3)]
    for job in jobs:
        queue(repo, job)

    assert repo.queue_position(jobs[0].id) == 1
    assert repo.queue_position(jobs[2].id) == 3

    repo.transition(jobs[0].id, JobStatus.PREPARING)
    assert repo.queue_position(jobs[0].id) is None  # no longer waiting
    assert repo.queue_position(jobs[1].id) == 1


def test_queue_snapshot_holds_active_jobs_in_order(repo: JobRepository) -> None:
    jobs = [repo.create(make_job()) for _ in range(3)]
    queue(repo, jobs[0])
    repo.transition(jobs[0].id, JobStatus.PREPARING)
    queue(repo, jobs[1])
    for status in (JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.GENERATING,
                   JobStatus.COMPLETED):
        repo.transition(jobs[2].id, status)

    snapshot = repo.queue_snapshot()
    assert [j.id for j in snapshot] == [jobs[0].id, jobs[1].id]
    assert repo.count_active() == 2


# ---------------- quotas ----------------


def test_daily_count_covers_todays_jobs(repo: JobRepository) -> None:
    for _ in range(3):
        repo.create(make_job(OWNER))
    repo.create(make_job(OTHER))

    assert repo.count_for_user_today(OWNER) == 3
    assert repo.count_for_user_today(OTHER) == 1


def test_cancelled_jobs_do_not_consume_quota(repo: JobRepository) -> None:
    job = repo.create(make_job(OWNER))
    repo.create(make_job(OWNER))
    assert repo.count_for_user_today(OWNER) == 2

    repo.transition(job.id, JobStatus.CANCELLED)
    assert repo.count_for_user_today(OWNER) == 1


def test_older_jobs_fall_outside_the_window(repo: JobRepository) -> None:
    job = repo.create(make_job(OWNER))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with repo._db.transaction() as connection:
        connection.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (old, job.id))

    assert repo.count_for_user_today(OWNER) == 0
    assert repo.count_for_user_since(OWNER, datetime.now(timezone.utc) - timedelta(days=3)) == 1


# ---------------- recovery ----------------


def test_interrupted_jobs_are_the_active_ones(repo: JobRepository) -> None:
    received = repo.create(make_job())
    generating = repo.create(make_job())
    for status in (JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.GENERATING):
        repo.transition(generating.id, status)
    done = repo.create(make_job())
    for status in (JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.GENERATING,
                   JobStatus.COMPLETED):
        repo.transition(done.id, status)

    interrupted = {j.id for j in repo.interrupted_jobs()}
    assert interrupted == {received.id, generating.id}


def test_stale_jobs_are_found_by_age(repo: JobRepository) -> None:
    job = repo.create(make_job())
    queue(repo, job)
    assert repo.stale_jobs(timedelta(hours=1)) == []

    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    with repo._db.transaction() as connection:
        connection.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (old, job.id))
    assert [j.id for j in repo.stale_jobs(timedelta(hours=1))] == [job.id]


def test_retry_count_increments(repo: JobRepository) -> None:
    job = repo.create(make_job())
    assert repo.increment_retry(job.id) == 1
    assert repo.increment_retry(job.id) == 2
    assert repo.get(job.id).retry_count == 2


# ---------------- filename prefix ----------------


def test_output_prefix_is_built_only_from_integers(repo: JobRepository) -> None:
    """User text must never reach a filename, even via the original request."""
    job = repo.create(make_job(original_request="../../.env; rm -rf /"))
    prefix = repo.get(job.id).output_prefix()
    assert prefix == f"job_{job.id:06d}_u{OWNER}"
    assert "/" not in prefix and "." not in prefix and " " not in prefix


def test_preparing_can_return_to_queued(repo: JobRepository) -> None:
    """Recovery requeues a job that was never submitted; the machine must allow it."""
    job = repo.create(make_job())
    queue(repo, job)
    repo.transition(job.id, JobStatus.PREPARING)
    assert repo.transition(job.id, JobStatus.QUEUED).status is JobStatus.QUEUED


def test_generating_can_never_return_to_queued(repo: JobRepository) -> None:
    """Requeueing a submitted job risks running two minutes of GPU work twice."""
    job = repo.create(make_job())
    queue(repo, job)
    repo.transition(job.id, JobStatus.PREPARING)
    repo.transition(job.id, JobStatus.GENERATING)
    assert not can_transition(JobStatus.GENERATING, JobStatus.QUEUED)
    with pytest.raises(InvalidTransition):
        repo.transition(job.id, JobStatus.QUEUED)
