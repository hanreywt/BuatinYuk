"""Async client for a local ComfyUI instance.

ComfyUI is treated as an external service that may be offline, slow, or restarted at
any moment. Nothing here writes to ComfyUI's installation - the client submits graphs,
watches execution, and downloads results.

Completion is determined by polling `/history`, which is authoritative and survives a
dropped socket. The WebSocket is used only for progress reporting, and any failure of
it is non-fatal.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import websockets

from app.comfy.errors import (
    ComfyExecutionFailed,
    ComfyInterrupted,
    ComfyOutputMissing,
    ComfyRejectedWorkflow,
    ComfyTimeout,
    ComfyUnavailable,
)
from app.comfy.models import OutputKind, OutputRef, Progress, QueueState, SystemStatus
from app.utils.logging import get_logger

log = get_logger(__name__)

ProgressCallback = Callable[[Progress], Awaitable[None]]

_POLL_INTERVAL = 2.0
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 30.0
_DOWNLOAD_TIMEOUT = 120.0
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


class ComfyUIClient:
    """One client per application. Not tied to a single job; safe to share."""

    def __init__(self, base_url: str, ws_url: str, *, client_id: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._ws_url = ws_url
        self._client_id = client_id or str(uuid.uuid4())
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
        )

    @property
    def client_id(self) -> str:
        return self._client_id

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ComfyUIClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------- health ----------------

    async def status(self) -> SystemStatus:
        """Never raises. Returns an offline status instead, so callers can report it."""
        try:
            stats = await self._get_json("/system_stats")
            queue = await self.queue_state()
        except Exception as exc:  # noqa: BLE001 - a health check must not propagate
            log.warning("comfy.status.unavailable", error=str(exc))
            return SystemStatus(online=False, error=str(exc))

        devices = stats.get("devices") or []
        primary = devices[0] if devices else {}
        return SystemStatus(
            online=True,
            version=(stats.get("system") or {}).get("comfyui_version"),
            devices=[d.get("name", "?") for d in devices],
            vram_free_gb=_to_gb(primary.get("vram_free")),
            vram_total_gb=_to_gb(primary.get("vram_total")),
            queue=queue,
        )

    async def is_online(self) -> bool:
        return (await self.status()).online

    async def queue_state(self) -> QueueState:
        data = await self._get_json("/queue")
        return QueueState(
            running=len(data.get("queue_running") or []),
            pending=len(data.get("queue_pending") or []),
        )

    # ---------------- submission ----------------

    async def submit(self, graph: dict[str, Any]) -> str:
        """POST an API-format graph. Returns ComfyUI's prompt_id."""
        payload = {"prompt": graph, "client_id": self._client_id}
        try:
            response = await self._http.post("/prompt", json=payload)
        except httpx.RequestError as exc:
            raise ComfyUnavailable(f"cannot reach ComfyUI at {self._base_url}: {exc}") from exc

        if response.status_code >= 400:
            raise ComfyRejectedWorkflow(_describe_rejection(response))

        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise ComfyRejectedWorkflow("ComfyUI accepted the graph but returned no prompt_id")

        log.info("comfy.submitted", prompt_id=prompt_id, nodes=len(graph))
        return prompt_id

    async def interrupt(self) -> None:
        """Stop whatever is currently executing.

        ComfyUI has no per-prompt interrupt, so callers must first confirm the running
        job is the one they mean to cancel.
        """
        try:
            await self._http.post("/interrupt")
        except httpx.RequestError as exc:
            raise ComfyUnavailable(str(exc)) from exc

    async def cancel_pending(self, prompt_id: str) -> None:
        """Remove a not-yet-started prompt from ComfyUI's queue."""
        try:
            await self._http.post("/queue", json={"delete": [prompt_id]})
        except httpx.RequestError as exc:
            raise ComfyUnavailable(str(exc)) from exc

    # ---------------- waiting ----------------

    async def wait(
        self,
        prompt_id: str,
        *,
        timeout: float,
        on_progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[OutputRef]:
        """Block until the prompt finishes, then return its outputs.

        `/history` is the source of truth. The WebSocket only feeds `on_progress`; if
        it drops, waiting continues uninterrupted.
        """
        watcher: asyncio.Task[None] | None = None
        if on_progress is not None:
            watcher = asyncio.create_task(self._watch_progress(prompt_id, on_progress))

        try:
            return await asyncio.wait_for(
                self._poll_until_done(prompt_id, cancelled), timeout=timeout
            )
        except TimeoutError as exc:
            log.warning("comfy.timeout", prompt_id=prompt_id, timeout=timeout)
            raise ComfyTimeout(f"prompt {prompt_id} exceeded {timeout}s") from exc
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    async def _poll_until_done(
        self, prompt_id: str, cancelled: Callable[[], bool] | None
    ) -> list[OutputRef]:
        consecutive_errors = 0
        while True:
            if cancelled is not None and cancelled():
                raise ComfyInterrupted(f"prompt {prompt_id} cancelled by request")

            try:
                entry = await self._history_entry(prompt_id)
                consecutive_errors = 0
            except ComfyUnavailable:
                # A ComfyUI restart mid-job should not lose the job outright; tolerate
                # a short outage before giving up.
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    raise
                await asyncio.sleep(_POLL_INTERVAL)
                continue

            if entry is not None:
                status = entry.get("status") or {}
                if status.get("completed") is True or status.get("status_str") == "success":
                    return _extract_outputs(entry)
                if status.get("status_str") == "error":
                    raise ComfyExecutionFailed(_describe_execution_error(status))

            await asyncio.sleep(_POLL_INTERVAL)

    async def _history_entry(self, prompt_id: str) -> dict[str, Any] | None:
        data = await self._get_json(f"/history/{prompt_id}")
        return data.get(prompt_id)

    async def _watch_progress(self, prompt_id: str, on_progress: ProgressCallback) -> None:
        """Best effort. Any failure is logged at debug and ends the watcher quietly."""
        url = f"{self._ws_url}?clientId={self._client_id}"
        try:
            async with websockets.connect(url, open_timeout=_CONNECT_TIMEOUT) as socket:
                async for raw in socket:
                    if not isinstance(raw, str):
                        continue  # binary frames are preview images; ignore them
                    progress = _parse_progress(raw, prompt_id)
                    if progress is not None:
                        await on_progress(progress)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - progress is optional by design
            log.debug("comfy.ws.unavailable", prompt_id=prompt_id, error=str(exc))

    # ---------------- results ----------------

    async def download(self, ref: OutputRef, destination: Path) -> Path:
        """Stream one output file to `destination`, which the caller has already proven
        safe. Refuses absurdly large files rather than filling the disk."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            async with self._http.stream(
                "GET", "/view", params=ref.query, timeout=_DOWNLOAD_TIMEOUT
            ) as response:
                if response.status_code >= 400:
                    raise ComfyOutputMissing(
                        f"ComfyUI returned {response.status_code} for {ref.filename}"
                    )
                with destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > _MAX_DOWNLOAD_BYTES:
                            raise ComfyOutputMissing("output exceeded the maximum allowed size")
                        handle.write(chunk)
        except httpx.RequestError as exc:
            destination.unlink(missing_ok=True)
            raise ComfyUnavailable(f"download failed: {exc}") from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        if written == 0:
            destination.unlink(missing_ok=True)
            raise ComfyOutputMissing(f"{ref.filename} was empty")

        log.info("comfy.downloaded", filename=ref.filename, bytes=written)
        return destination

    # ---------------- internals ----------------

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = await self._http.get(path)
        except httpx.RequestError as exc:
            raise ComfyUnavailable(f"GET {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise ComfyUnavailable(f"GET {path} returned {response.status_code}")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ComfyUnavailable(f"GET {path} returned non-JSON") from exc


def _to_gb(value: object) -> float | None:
    return round(value / 2**30, 2) if isinstance(value, (int, float)) else None


def _parse_progress(raw: str, prompt_id: str) -> Progress | None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None

    kind = message.get("type")
    data = message.get("data") or {}

    # A message carrying a different prompt_id belongs to somebody else's job.
    if data.get("prompt_id") not in (None, prompt_id):
        return None

    if kind == "progress":
        return Progress(step=data.get("value"), total_steps=data.get("max"))
    if kind == "executing":
        return Progress(node=data.get("node"))
    if kind == "status":
        exec_info = ((data.get("status") or {}).get("exec_info")) or {}
        return Progress(queue_remaining=exec_info.get("queue_remaining"))
    return None


def _extract_outputs(entry: dict[str, Any]) -> list[OutputRef]:
    refs: list[OutputRef] = []
    for node_output in (entry.get("outputs") or {}).values():
        for kind in OutputKind:
            for item in node_output.get(kind.value) or []:
                filename = item.get("filename")
                if not filename:
                    continue
                refs.append(
                    OutputRef(
                        filename=filename,
                        subfolder=item.get("subfolder") or "",
                        type=item.get("type") or "output",
                        kind=kind,
                    )
                )
    if not refs:
        raise ComfyOutputMissing("execution reported success but produced no files")
    return refs


def _describe_rejection(response: httpx.Response) -> str:
    """Flatten ComfyUI's node_errors into one log-friendly line."""
    try:
        body = response.json()
    except json.JSONDecodeError:
        return f"HTTP {response.status_code}: {response.text[:300]}"

    error = body.get("error") or {}
    parts = [str(error.get("message") or f"HTTP {response.status_code}")]
    if error.get("details"):
        parts.append(str(error["details"]))
    for node_id, node_error in (body.get("node_errors") or {}).items():
        for detail in node_error.get("errors") or []:
            parts.append(f"node {node_id}: {detail.get('message')} ({detail.get('details')})")
    return " | ".join(parts)[:1000]


def _describe_execution_error(status: dict[str, Any]) -> str:
    for entry in status.get("messages") or []:
        if isinstance(entry, list) and entry and entry[0] == "execution_error":
            data = entry[1] if len(entry) > 1 else {}
            return (
                f"node {data.get('node_id')} ({data.get('node_type')}): "
                f"{data.get('exception_type')}: {data.get('exception_message')}"
            )[:1000]
    return "execution failed without further detail"
