"""A small local dashboard for watching and steering the queue.

Deliberately local-only. It binds to loopback and refuses to start on any other
address: this exposes the queue, job history, and worker controls, none of which
should ever be reachable from the network. There is no login, because there is no
remote access to authenticate.

It runs inside the bot's own process and event loop, so pausing the worker or
cancelling a job acts on the live objects rather than on a copy of the state.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from app.comfy.client import ComfyUIClient
from app.jobs.models import JobStatus
from app.jobs.repository import JobRepository
from app.orchestrator.worker import GenerationWorker
from app.users.models import Role
from app.users.repository import InviteError
from app.users.service import UserService
from app.utils.logging import get_logger

log = get_logger(__name__)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DASHBOARD = Path(__file__).parent / "dashboard.html"


class Dashboard:
    """Serves the dashboard page and a small JSON API over it."""

    def __init__(
        self,
        *,
        jobs: JobRepository,
        worker: GenerationWorker,
        comfy: ComfyUIClient,
        users: UserService | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        if host not in LOOPBACK:
            raise ValueError(
                f"dashboard host is {host!r}. It must stay on loopback: it exposes the "
                "queue and worker controls with no authentication."
            )
        self._jobs = jobs
        self._worker = worker
        self._comfy = comfy
        self._users = users
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._page),
                web.get("/api/state", self._state),
                web.post("/api/worker/pause", self._pause),
                web.post("/api/worker/resume", self._resume),
                web.post("/api/jobs/{job_id}/cancel", self._cancel),
                web.get("/api/users", self._list_users),
                web.post("/api/users", self._add_user),
                web.post("/api/users/{user_id}/enabled", self._set_enabled),
                web.post("/api/users/{user_id}/quota", self._set_quota),
                web.delete("/api/users/{user_id}", self._remove_user),
                web.get("/api/invites", self._list_invites),
                web.post("/api/invites", self._create_invite),
                web.delete("/api/invites/{code}", self._revoke_invite),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
        except OSError as exc:
            # A busy port should not stop the bot; the dashboard is a convenience.
            log.warning("dashboard.bind_failed", url=self.url, error=str(exc))
            await self._runner.cleanup()
            self._runner = None
            return
        log.info("dashboard.started", url=self.url)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            log.info("dashboard.stopped")

    # ---------------- handlers ----------------

    async def _page(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(DASHBOARD)

    async def _state(self, _request: web.Request) -> web.Response:
        comfy = await self._comfy.status()
        active = self._jobs.queue_snapshot()
        recent = self._jobs.recent(limit=20)

        return web.json_response(
            {
                "comfyui": {
                    "online": comfy.online,
                    "version": comfy.version,
                    "vram_free_gb": comfy.vram_free_gb,
                    "vram_total_gb": comfy.vram_total_gb,
                    "device": comfy.devices[0] if comfy.devices else None,
                },
                "worker": {
                    "running": self._worker.is_running,
                    "paused": self._worker.is_paused,
                    "busy": self._worker.is_busy,
                    "current_job_id": self._worker.current_job_id,
                },
                "queue": [self._describe(job) for job in active],
                "recent": [self._describe(job) for job in recent],
                "stats": self._stats(),
            }
        )

    async def _pause(self, _request: web.Request) -> web.Response:
        self._worker.pause()
        return web.json_response({"paused": True})

    async def _resume(self, _request: web.Request) -> web.Response:
        self._worker.resume()
        return web.json_response({"paused": False})

    async def _cancel(self, request: web.Request) -> web.Response:
        raw = request.match_info["job_id"]
        if not raw.isdigit():
            raise web.HTTPBadRequest(text="job id must be a number")
        job_id = int(raw)

        # The dashboard is an operator tool, so it is not scoped to one owner - but it
        # still goes through the same request/interrupt path the bot uses.
        cancelled = self._jobs.request_cancel(job_id)
        if cancelled and self._worker.current_job_id == job_id:
            try:
                await self._comfy.interrupt()
            except Exception as exc:  # noqa: BLE001 - cancelling is best effort
                log.warning("dashboard.interrupt_failed", job_id=job_id, error=str(exc))
        elif cancelled:
            for expected in (JobStatus.QUEUED, JobStatus.RECEIVED):
                try:
                    self._jobs.transition(
                        job_id, JobStatus.CANCELLED, expect=expected,
                        user_message="Cancelled.",
                    )
                except Exception:  # noqa: BLE001 - the worker claimed it; it will handle it
                    continue
                break
        return web.json_response({"cancelled": cancelled})

    # ---------------- people ----------------

    def _require_users(self) -> UserService:
        if self._users is None:
            raise web.HTTPServiceUnavailable(text="user management is not configured")
        return self._users

    def _require_store(self):
        store = self._require_users().store
        if store is None:
            raise web.HTTPServiceUnavailable(text="no user store is configured")
        return store

    async def _list_users(self, _request: web.Request) -> web.Response:
        service = self._require_users()
        used_today = self._jobs.count_for_user_today
        return web.json_response(
            {
                "users": [
                    {
                        "id": u.telegram_user_id,
                        "role": u.role.value,
                        "enabled": u.enabled,
                        "quota": None if u.has_unlimited_quota else u.daily_quota,
                        "used_today": used_today(u.telegram_user_id),
                        "name": u.display_name,
                        # A configured owner cannot be edited away from here. Saying so
                        # is clearer than letting the buttons fail.
                        "from_config": service.is_bootstrap_admin(u.telegram_user_id),
                    }
                    for u in service.list_users()
                ],
                "roles": [r.value for r in Role],
            }
        )

    async def _add_user(self, request: web.Request) -> web.Response:
        store = self._require_store()
        body = await _json_body(request)

        user_id = _positive_int(body.get("id"), "Telegram ID")
        store.upsert(
            user_id,
            role=_role(body.get("role", "user")),
            daily_quota=_positive_int(body.get("quota", 10), "quota", allow_zero=True),
            display_name=(body.get("name") or None),
            note=(body.get("note") or None),
        )
        return web.json_response({"added": user_id})

    async def _set_enabled(self, request: web.Request) -> web.Response:
        store = self._require_store()
        user_id = _positive_int(request.match_info["user_id"], "Telegram ID")
        enabled = bool((await _json_body(request)).get("enabled", True))
        if not store.set_enabled(user_id, enabled):
            raise web.HTTPNotFound(text="no such user")
        return web.json_response({"id": user_id, "enabled": enabled})

    async def _set_quota(self, request: web.Request) -> web.Response:
        store = self._require_store()
        user_id = _positive_int(request.match_info["user_id"], "Telegram ID")
        quota = _positive_int(
            (await _json_body(request)).get("quota"), "quota", allow_zero=True
        )
        if not store.set_quota(user_id, quota):
            raise web.HTTPNotFound(text="no such user")
        return web.json_response({"id": user_id, "quota": quota})

    async def _remove_user(self, request: web.Request) -> web.Response:
        service = self._require_users()
        user_id = _positive_int(request.match_info["user_id"], "Telegram ID")
        if service.is_bootstrap_admin(user_id):
            raise web.HTTPBadRequest(
                text="This owner comes from ADMIN_TELEGRAM_IDS in .env and must be "
                "removed there, not here."
            )
        if not self._require_store().remove(user_id):
            raise web.HTTPNotFound(text="no such user")
        return web.json_response({"removed": user_id})

    async def _list_invites(self, _request: web.Request) -> web.Response:
        store = self._require_store()
        return web.json_response(
            {
                "invites": [
                    {
                        "code": i.code,
                        "role": i.role.value,
                        "quota": i.daily_quota,
                        "state": i.state(),
                        "used_by": i.used_by,
                        "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                        "note": i.note,
                    }
                    for i in store.list_invites()
                ]
            }
        )

    async def _create_invite(self, request: web.Request) -> web.Response:
        service = self._require_users()
        store = self._require_store()
        body = await _json_body(request)

        admins = service.list_admins()
        try:
            invite = store.create_invite(
                created_by=admins[0] if admins else 0,
                role=_role(body.get("role", "user")),
                daily_quota=_positive_int(body.get("quota", 10), "quota", allow_zero=True),
                note=(body.get("note") or None),
                valid_days=_positive_int(body.get("days", 7), "days"),
            )
        except InviteError as exc:
            raise web.HTTPBadRequest(text=exc.user_message) from exc

        return web.json_response(
            {"code": invite.code, "expires_at": invite.expires_at.isoformat()}
        )

    async def _revoke_invite(self, request: web.Request) -> web.Response:
        if not self._require_store().revoke_invite(request.match_info["code"]):
            raise web.HTTPNotFound(text="no such unused invite")
        return web.json_response({"revoked": request.match_info["code"]})

    # ---------------- shaping ----------------

    def _describe(self, job: Any) -> dict[str, Any]:
        """Everything the page needs about one job, and nothing sensitive.

        No filesystem paths and no internal error text - the dashboard is local, but
        there is no reason for it to carry either.
        """
        return {
            "id": job.id,
            "status": job.status.value,
            "workflow": job.workflow_id,
            "kind": _kind_of(job.workflow_id),
            "user_id": job.telegram_user_id,
            "request": job.original_request[:120],
            "queue_position": (
                self._jobs.queue_position(job.id)
                if job.status is JobStatus.QUEUED
                else None
            ),
            "seconds": round(job.duration_seconds) if job.duration_seconds else None,
            "outputs": len(job.outputs),
            "created_at": job.created_at.astimezone(timezone.utc).isoformat(),
            "user_message": job.user_message,
        }

    def _stats(self) -> dict[str, Any]:
        """Average runtime per workflow, so the page can show a real expectation
        rather than a guess."""
        averages = self._jobs.average_duration_by_workflow()
        return {
            "averages": [
                {"workflow": name, "seconds": round(seconds), "runs": runs}
                for name, seconds, runs in averages
            ],
            "completed_today": self._jobs.count_completed_today(),
        }


def _kind_of(workflow_id: str) -> str:
    return "video" if "video" in workflow_id else "image"


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - any malformed body is the same problem
        raise web.HTTPBadRequest(text="expected a JSON object") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return body


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    """Telegram IDs and quotas are numbers. Anything else is a mistake, not a value."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"{label} must be a number") from None
    if number < 0 or (number == 0 and not allow_zero):
        raise web.HTTPBadRequest(text=f"{label} must be a positive number") from None
    return number


def _role(value: Any) -> Role:
    try:
        return Role(str(value).strip().lower())
    except ValueError:
        raise web.HTTPBadRequest(
            text=f"role must be one of: {', '.join(r.value for r in Role)}"
        ) from None
