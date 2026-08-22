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
