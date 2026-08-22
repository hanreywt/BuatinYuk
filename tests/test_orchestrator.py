"""Authorisation, quotas, and the orchestrator's submission gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.comfy.models import QueueState, SystemStatus
from app.database.connection import Database
from app.jobs.models import Job, JobStatus
from app.jobs.repository import JobRepository
from app.orchestrator.service import GenerationRequest, Orchestrator
from app.users.models import Role, User
from app.users.service import (
    AccountDisabled,
    NotAuthorized,
    QuotaExceeded,
    UserService,
)
from app.workflows.registry import ParameterError, WorkflowNotFound, WorkflowRegistry

ADMIN = 111
STRANGER = 999
FRIEND = 222


@pytest.fixture
def repo(tmp_path: Path):
    database = Database(tmp_path / "orc.db").connect()
    try:
        yield JobRepository(database)
    finally:
        database.close()


@pytest.fixture
def users(repo: JobRepository) -> UserService:
    return UserService(admin_ids=[ADMIN], jobs=repo, default_daily_quota=5)


class FakeWorker:
    def __init__(self) -> None:
        self.current_job_id = None
        self.is_running = True
        self.is_busy = False


class FakeComfy:
    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.interrupted = 0

    async def status(self) -> SystemStatus:
        return SystemStatus(
            online=self.online,
            version="0.33.2" if self.online else None,
            queue=QueueState(0, 0),
            vram_free_gb=2.0,
            vram_total_gb=10.0,
        )

    async def interrupt(self) -> None:
        self.interrupted += 1


@pytest.fixture
def orchestrator(repo, users, workflow_dir: Path):
    return Orchestrator(
        users=users,
        jobs=repo,
        registry=WorkflowRegistry.load(workflow_dir, strict=True),
        worker=FakeWorker(),
        comfy=FakeComfy(),
        default_workflow="test_wf",
    )


def request(user_id: int = ADMIN, text: str = "a city at night", **kwargs):
    return GenerationRequest(
        telegram_user_id=user_id, telegram_chat_id=user_id, text=text, **kwargs
    )


# ---------------- authorisation ----------------


def test_configured_admin_is_authorised(users: UserService) -> None:
    user = users.authorise(ADMIN)
    assert user.role is Role.ADMIN and user.is_admin and user.has_unlimited_quota


def test_stranger_is_refused(users: UserService) -> None:
    with pytest.raises(NotAuthorized) as exc:
        users.authorise(STRANGER)
    assert "not authorised" in exc.value.user_message.lower()


def test_refusal_message_reveals_nothing_about_the_system(users: UserService) -> None:
    with pytest.raises(NotAuthorized) as exc:
        users.authorise(STRANGER)
    message = exc.value.user_message
    for leak in ("admin", "127.0.0.1", "comfy", "workflow", str(ADMIN)):
        assert leak not in message.lower()


def test_non_admin_gets_the_same_refusal_as_a_stranger(repo: JobRepository) -> None:
    """An ordinary user must not learn that admin commands exist."""
    service = UserService(admin_ids=[ADMIN], jobs=repo)
    with pytest.raises(NotAuthorized) as stranger:
        service.authorise_admin(STRANGER)
    with pytest.raises(NotAuthorized) as ordinary:
        service.authorise_admin(FRIEND)
    assert stranger.value.user_message == ordinary.value.user_message


def test_disabled_account_is_refused(repo: JobRepository, monkeypatch) -> None:
    service = UserService(admin_ids=[ADMIN], jobs=repo)
    monkeypatch.setattr(
        service, "_lookup", lambda uid: User(telegram_user_id=uid, enabled=False)
    )
    with pytest.raises(AccountDisabled):
        service.authorise(ADMIN)


def test_username_is_never_the_identity() -> None:
    """A display name is decoration; the id is what counts."""
    user = User(telegram_user_id=555, display_name="admin")
    assert user.role is Role.USER
    assert not user.is_admin
    assert "555" in user.label()


# ---------------- quotas ----------------


def test_admin_quota_is_unlimited(users: UserService, repo: JobRepository) -> None:
    user = users.authorise(ADMIN)
    for _ in range(50):
        repo.create(Job(telegram_user_id=ADMIN, telegram_chat_id=ADMIN,
                        original_request="x", workflow_id="test_wf"))
    quota = users.check_quota(user)
    assert quota.unlimited and quota.remaining is None
    assert "no limit" in quota.describe()


def test_quota_counts_down_and_then_refuses(repo: JobRepository) -> None:
    service = UserService(admin_ids=[], jobs=repo, default_daily_quota=2)
    user = User(telegram_user_id=FRIEND, role=Role.USER, daily_quota=2)

    assert service.check_quota(user).remaining == 2
    repo.create(Job(telegram_user_id=FRIEND, telegram_chat_id=FRIEND,
                    original_request="x", workflow_id="test_wf"))
    assert service.check_quota(user).remaining == 1
    repo.create(Job(telegram_user_id=FRIEND, telegram_chat_id=FRIEND,
                    original_request="x", workflow_id="test_wf"))

    with pytest.raises(QuotaExceeded) as exc:
        service.check_quota(user)
    assert "daily limit of 2" in exc.value.user_message


def test_quota_status_never_raises(repo: JobRepository) -> None:
    service = UserService(admin_ids=[], jobs=repo, default_daily_quota=1)
    user = User(telegram_user_id=FRIEND, daily_quota=1)
    repo.create(Job(telegram_user_id=FRIEND, telegram_chat_id=FRIEND,
                    original_request="x", workflow_id="test_wf"))
    assert service.quota_status(user).remaining == 0


def test_one_users_jobs_do_not_consume_anothers_quota(repo: JobRepository) -> None:
    service = UserService(admin_ids=[], jobs=repo, default_daily_quota=2)
    for _ in range(2):
        repo.create(Job(telegram_user_id=STRANGER, telegram_chat_id=STRANGER,
                        original_request="x", workflow_id="test_wf"))
    assert service.check_quota(User(telegram_user_id=FRIEND, daily_quota=2)).remaining == 2


# ---------------- submission ----------------


async def test_submit_creates_and_queues_a_job(orchestrator, repo) -> None:
    accepted = await orchestrator.submit(request())

    assert accepted.job.id is not None
    assert repo.get(accepted.job.id).status is JobStatus.QUEUED
    assert accepted.queue_position == 1
    assert "accepted" in accepted.describe().lower()


async def test_submit_from_a_stranger_is_refused_and_creates_nothing(
    orchestrator, repo
) -> None:
    with pytest.raises(NotAuthorized):
        await orchestrator.submit(request(STRANGER))
    assert repo.count_active() == 0


async def test_queue_position_reflects_jobs_ahead(orchestrator) -> None:
    first = await orchestrator.submit(request())
    second = await orchestrator.submit(request())
    assert first.queue_position == 1
    assert second.queue_position == 2
    assert second.ahead_of_it == 1


async def test_empty_request_is_refused(orchestrator) -> None:
    with pytest.raises(ParameterError, match="empty"):
        await orchestrator.submit(request(text="    "))


async def test_overlong_request_is_refused(orchestrator) -> None:
    with pytest.raises(ParameterError):
        await orchestrator.submit(request(text="a" * 5000))


async def test_invalid_parameters_are_caught_at_submit_time(orchestrator, repo) -> None:
    """Better to refuse now than to fail two minutes into a generation."""
    with pytest.raises(ParameterError):
        await orchestrator.submit(request(parameters={"nonexistent": 1}))
    assert repo.count_active() == 0


async def test_unknown_workflow_is_refused(orchestrator) -> None:
    with pytest.raises(WorkflowNotFound):
        await orchestrator.submit(request(workflow_id="no_such_wf"))


async def test_prompt_is_stored_as_given(orchestrator, repo) -> None:
    accepted = await orchestrator.submit(request(text="  a red bicycle  "))
    job = repo.get(accepted.job.id)
    assert job.original_request == "a red bicycle"
    assert job.parameters["prompt"] == "a red bicycle"


# ---------------- ownership on queries ----------------


async def test_a_user_cannot_read_another_users_job(orchestrator, repo) -> None:
    accepted = await orchestrator.submit(request(ADMIN))
    # A different authorised user would see nothing; a stranger is refused outright.
    with pytest.raises(NotAuthorized):
        await orchestrator.job_for_user(accepted.job.id, STRANGER)


async def test_history_is_scoped_to_the_caller(orchestrator, repo) -> None:
    await orchestrator.submit(request(ADMIN))
    repo.create(Job(telegram_user_id=FRIEND, telegram_chat_id=FRIEND,
                    original_request="theirs", workflow_id="test_wf"))

    history = await orchestrator.history(ADMIN)
    assert all(job.telegram_user_id == ADMIN for job in history)
    assert len(history) == 1


async def test_admin_sees_the_whole_queue(orchestrator, repo) -> None:
    await orchestrator.submit(request(ADMIN))
    other = repo.create(Job(telegram_user_id=FRIEND, telegram_chat_id=FRIEND,
                            original_request="theirs", workflow_id="test_wf"))
    repo.transition(other.id, JobStatus.QUEUED)

    assert len(await orchestrator.queue_view(ADMIN)) == 2


async def test_non_admin_queue_view_is_filtered(repo, workflow_dir: Path) -> None:
    service = UserService(admin_ids=[], jobs=repo)
    service._lookup = lambda uid: User(telegram_user_id=uid, role=Role.USER)  # type: ignore[method-assign]
    orchestrator = Orchestrator(
        users=service,
        jobs=repo,
        registry=WorkflowRegistry.load(workflow_dir, strict=True),
        worker=FakeWorker(),
        comfy=FakeComfy(),
        default_workflow="test_wf",
    )
    mine = repo.create(Job(telegram_user_id=FRIEND, telegram_chat_id=FRIEND,
                           original_request="mine", workflow_id="test_wf"))
    repo.transition(mine.id, JobStatus.QUEUED)
    theirs = repo.create(Job(telegram_user_id=STRANGER, telegram_chat_id=STRANGER,
                             original_request="theirs", workflow_id="test_wf"))
    repo.transition(theirs.id, JobStatus.QUEUED)

    visible = await orchestrator.queue_view(FRIEND)
    assert [job.id for job in visible] == [mine.id]


# ---------------- cancellation ----------------


async def test_owner_can_cancel_their_job(orchestrator, repo) -> None:
    accepted = await orchestrator.submit(request(ADMIN))
    assert await orchestrator.cancel(accepted.job.id, ADMIN) is True
    assert repo.is_cancel_requested(accepted.job.id)


async def test_cancel_only_interrupts_when_that_job_holds_the_gpu(
    orchestrator, repo
) -> None:
    """Interrupting ComfyUI for a queued job would kill somebody else's generation."""
    accepted = await orchestrator.submit(request(ADMIN))
    comfy = orchestrator._comfy

    await orchestrator.cancel(accepted.job.id, ADMIN)
    assert comfy.interrupted == 0  # it was only queued

    orchestrator._worker.current_job_id = accepted.job.id
    second = await orchestrator.submit(request(ADMIN))
    await orchestrator.cancel(accepted.job.id, ADMIN)
    assert comfy.interrupted == 1
    del second


async def test_cancelling_an_unknown_job_reports_false(orchestrator) -> None:
    assert await orchestrator.cancel(4321, ADMIN) is False


# ---------------- status ----------------


async def test_system_status_reports_components(orchestrator) -> None:
    status = await orchestrator.system_status(ADMIN)
    assert status.comfyui_online is True
    assert status.worker_running is True
    assert "test_wf" in status.workflows
    assert "online" in status.describe()


async def test_system_status_reports_comfyui_offline(repo, users, workflow_dir: Path) -> None:
    orchestrator = Orchestrator(
        users=users,
        jobs=repo,
        registry=WorkflowRegistry.load(workflow_dir, strict=True),
        worker=FakeWorker(),
        comfy=FakeComfy(online=False),
        default_workflow="test_wf",
    )
    status = await orchestrator.system_status(ADMIN)
    assert status.comfyui_online is False
    assert "OFFLINE" in status.describe()


async def test_status_requires_authorisation(orchestrator) -> None:
    with pytest.raises(NotAuthorized):
        await orchestrator.system_status(STRANGER)


async def test_each_job_gets_its_own_seed(orchestrator, repo) -> None:
    """A baked-in seed made the same prompt produce a byte-identical image forever."""
    seeds = set()
    for _ in range(5):
        accepted = await orchestrator.submit(request(text="same prompt every time"))
        seeds.add(repo.get(accepted.job.id).parameters["seed"])
    assert len(seeds) == 5


async def test_an_explicit_seed_is_respected(orchestrator, repo) -> None:
    """Reproducing an earlier result must stay possible."""
    accepted = await orchestrator.submit(request(parameters={"seed": 12345}))
    assert repo.get(accepted.job.id).parameters["seed"] == 12345


async def test_the_seed_is_recorded_so_a_result_can_be_reproduced(orchestrator, repo) -> None:
    accepted = await orchestrator.submit(request())
    seed = repo.get(accepted.job.id).parameters["seed"]
    assert isinstance(seed, int) and 0 <= seed < 2**32


# ---------------- workflow routing ----------------


@pytest.fixture
def routing_orchestrator(repo, users, workflow_dir: Path):
    """Registry with four distinct ids, so routing choices are observable."""
    import json
    from pathlib import Path as P

    graph = json.loads((workflow_dir / "test_wf.api.json").read_text(encoding="utf-8"))
    meta = json.loads((workflow_dir / "test_wf.meta.json").read_text(encoding="utf-8"))
    for wf_id in ("test_img", "test_vid", "test_imgvid"):
        (workflow_dir / f"{wf_id}.api.json").write_text(json.dumps(graph), encoding="utf-8")
        (workflow_dir / f"{wf_id}.meta.json").write_text(
            json.dumps({**meta, "workflow_id": wf_id, "graph_file": f"{wf_id}.api.json"}),
            encoding="utf-8",
        )
    return Orchestrator(
        users=users,
        jobs=repo,
        registry=WorkflowRegistry.load(workflow_dir, strict=True),
        worker=FakeWorker(),
        comfy=FakeComfy(),
        default_workflow="test_wf",
        uploads=None,
        image_workflow="test_img",
        video_workflow="test_vid",
        image_video_workflow="test_imgvid",
    )


async def test_text_alone_routes_to_the_default_workflow(routing_orchestrator, repo) -> None:
    accepted = await routing_orchestrator.submit(request())
    assert repo.get(accepted.job.id).workflow_id == "test_wf"


async def test_text_asking_for_video_routes_to_the_video_workflow(
    routing_orchestrator, repo
) -> None:
    accepted = await routing_orchestrator.submit(request(want_video=True))
    assert repo.get(accepted.job.id).workflow_id == "test_vid"


async def test_an_explicit_workflow_id_overrides_routing(routing_orchestrator, repo) -> None:
    accepted = await routing_orchestrator.submit(
        request(want_video=True, workflow_id="test_wf")
    )
    assert repo.get(accepted.job.id).workflow_id == "test_wf"


async def test_an_image_without_an_upload_service_is_refused(routing_orchestrator) -> None:
    """Better a clear refusal than a job that cannot possibly run."""
    with pytest.raises(ParameterError, match="upload service"):
        await routing_orchestrator.submit(request(image=b"\x89PNG\r\n\x1a\n" + b"\x00" * 200))
