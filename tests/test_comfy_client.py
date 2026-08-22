"""ComfyUI client behaviour, driven by a mock transport - no GPU, no real ComfyUI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.comfy.client import ComfyUIClient, _describe_execution_error, _extract_outputs
from app.comfy.errors import (
    ComfyExecutionFailed,
    ComfyOutputMissing,
    ComfyRejectedWorkflow,
    ComfyTimeout,
    ComfyUnavailable,
)
from app.comfy.models import OutputKind, OutputRef

BASE = "http://127.0.0.1:8188"
WS = "ws://127.0.0.1:8188/ws"


def client_with(handler) -> ComfyUIClient:
    client = ComfyUIClient(BASE, WS)
    client._http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    return client


def success_history(prompt_id: str, files: list[str]) -> dict[str, Any]:
    return {
        prompt_id: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "13": {"images": [{"filename": f, "subfolder": "", "type": "output"}
                                  for f in files]}
            },
        }
    }


# ---------------- submission ----------------


async def test_submit_returns_prompt_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "client_id" in payload and "prompt" in payload
        return httpx.Response(200, json={"prompt_id": "abc-123"})

    async with client_with(handler) as client:
        assert await client.submit({"1": {"class_type": "X", "inputs": {}}}) == "abc-123"


async def test_rejected_graph_surfaces_node_errors_in_the_log_message() -> None:
    body = {
        "error": {"message": "Prompt outputs failed validation", "details": ""},
        "node_errors": {
            "4": {"errors": [{"message": "value not in list",
                              "details": "unet_name: 'missing.safetensors'"}]}
        },
    }

    async with client_with(lambda r: httpx.Response(400, json=body)) as client:
        with pytest.raises(ComfyRejectedWorkflow) as exc:
            await client.submit({"4": {"class_type": "UNETLoader", "inputs": {}}})

    assert "node 4" in str(exc.value)
    assert "missing.safetensors" in str(exc.value)
    # The user never sees the node id or the model filename.
    assert "missing.safetensors" not in exc.value.user_message
    assert "node" not in exc.value.user_message.lower()


async def test_unreachable_comfyui_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with client_with(handler) as client:
        with pytest.raises(ComfyUnavailable):
            await client.submit({"1": {"class_type": "X", "inputs": {}}})


async def test_accepted_but_missing_prompt_id_is_rejected() -> None:
    async with client_with(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ComfyRejectedWorkflow, match="no prompt_id"):
            await client.submit({"1": {"class_type": "X", "inputs": {}}})


# ---------------- status ----------------


async def test_status_reports_offline_instead_of_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with client_with(handler) as client:
        status = await client.status()

    assert status.online is False
    assert status.error
    assert status.version is None


async def test_status_parses_devices_and_queue() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={
                "system": {"comfyui_version": "0.33.2"},
                "devices": [{"name": "cuda:0 RTX 3080", "vram_total": 2**30 * 10,
                             "vram_free": 2**30 * 2}],
            })
        return httpx.Response(200, json={"queue_running": [1], "queue_pending": [1, 2]})

    async with client_with(handler) as client:
        status = await client.status()

    assert status.online and status.version == "0.33.2"
    assert status.vram_total_gb == 10.0 and status.vram_free_gb == 2.0
    assert status.queue.running == 1 and status.queue.pending == 2
    assert status.queue.total == 3 and status.queue.busy is True


# ---------------- waiting ----------------


async def test_wait_returns_outputs_once_history_reports_success(monkeypatch) -> None:
    monkeypatch.setattr("app.comfy.client._POLL_INTERVAL", 0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={})  # still running
        return httpx.Response(200, json=success_history("p1", ["a.png", "b.png"]))

    async with client_with(handler) as client:
        outputs = await client.wait("p1", timeout=5)

    assert [o.filename for o in outputs] == ["a.png", "b.png"]
    assert calls["n"] >= 3


async def test_wait_raises_execution_failed_on_error_status(monkeypatch) -> None:
    monkeypatch.setattr("app.comfy.client._POLL_INTERVAL", 0.01)
    history = {
        "p1": {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_error", {
                    "node_id": "4", "node_type": "UNETLoader",
                    "exception_type": "OutOfMemoryError",
                    "exception_message": "CUDA out of memory at D:\\models\\x.safetensors",
                }]],
            },
            "outputs": {},
        }
    }

    async with client_with(lambda r: httpx.Response(200, json=history)) as client:
        with pytest.raises(ComfyExecutionFailed) as exc:
            await client.wait("p1", timeout=5)

    assert "OutOfMemoryError" in str(exc.value)
    # No path, no node id, no exception type reaches the user.
    assert "D:\\" not in exc.value.user_message
    assert "OutOfMemory" not in exc.value.user_message


async def test_wait_times_out(monkeypatch) -> None:
    monkeypatch.setattr("app.comfy.client._POLL_INTERVAL", 0.01)
    async with client_with(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ComfyTimeout):
            await client.wait("p1", timeout=0.1)


async def test_wait_tolerates_a_brief_comfyui_outage(monkeypatch) -> None:
    """A restart mid-job must not immediately fail the job."""
    monkeypatch.setattr("app.comfy.client._POLL_INTERVAL", 0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise httpx.ConnectError("restarting")
        return httpx.Response(200, json=success_history("p1", ["a.png"]))

    async with client_with(handler) as client:
        assert len(await client.wait("p1", timeout=5)) == 1


async def test_wait_gives_up_after_a_sustained_outage(monkeypatch) -> None:
    monkeypatch.setattr("app.comfy.client._POLL_INTERVAL", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gone")

    async with client_with(handler) as client:
        with pytest.raises(ComfyUnavailable):
            await client.wait("p1", timeout=5)


async def test_wait_honours_a_cancellation_check(monkeypatch) -> None:
    monkeypatch.setattr("app.comfy.client._POLL_INTERVAL", 0.01)
    from app.comfy.errors import ComfyInterrupted

    async with client_with(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ComfyInterrupted):
            await client.wait("p1", timeout=5, cancelled=lambda: True)


# ---------------- outputs ----------------


def test_extract_outputs_reads_every_media_kind() -> None:
    entry = {
        "outputs": {
            "9": {"images": [{"filename": "a.png", "subfolder": "s", "type": "output"}]},
            "10": {"gifs": [{"filename": "b.mp4", "subfolder": "", "type": "output"}]},
        }
    }
    refs = _extract_outputs(entry)
    assert {r.kind for r in refs} == {OutputKind.IMAGE, OutputKind.GIF}
    assert refs[0].query == {"filename": "a.png", "subfolder": "s", "type": "output"}


def test_extract_outputs_raises_when_nothing_was_produced() -> None:
    with pytest.raises(ComfyOutputMissing):
        _extract_outputs({"outputs": {}})


def test_execution_error_description_handles_missing_detail() -> None:
    assert "without further detail" in _describe_execution_error({"messages": []})


async def test_download_streams_to_disk(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n" + b"x" * 5000

    async with client_with(lambda r: httpx.Response(200, content=payload)) as client:
        result = await client.download(OutputRef("a.png", "", "output"), tmp_path / "a.png")

    assert result.read_bytes() == payload


async def test_download_removes_the_partial_file_on_failure(tmp_path: Path) -> None:
    destination = tmp_path / "a.png"

    async with client_with(lambda r: httpx.Response(404)) as client:
        with pytest.raises(ComfyOutputMissing):
            await client.download(OutputRef("a.png", "", "output"), destination)

    assert not destination.exists()


async def test_empty_download_is_treated_as_missing(tmp_path: Path) -> None:
    destination = tmp_path / "a.png"

    async with client_with(lambda r: httpx.Response(200, content=b"")) as client:
        with pytest.raises(ComfyOutputMissing):
            await client.download(OutputRef("a.png", "", "output"), destination)

    assert not destination.exists()


async def test_oversized_download_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.comfy.client._MAX_DOWNLOAD_BYTES", 1000)
    destination = tmp_path / "big.png"

    async with client_with(lambda r: httpx.Response(200, content=b"x" * 5000)) as client:
        with pytest.raises(ComfyOutputMissing, match="maximum allowed size"):
            await client.download(OutputRef("big.png", "", "output"), destination)

    assert not destination.exists()


# ---------------- progress parsing ----------------


def test_progress_messages_for_other_jobs_are_ignored() -> None:
    from app.comfy.client import _parse_progress

    mine = json.dumps({"type": "progress", "data": {"value": 3, "max": 12, "prompt_id": "p1"}})
    theirs = json.dumps({"type": "progress", "data": {"value": 9, "max": 12, "prompt_id": "p2"}})

    assert _parse_progress(mine, "p1").percent == 25
    assert _parse_progress(theirs, "p1") is None
    assert _parse_progress("not json", "p1") is None


def test_status_message_yields_queue_depth() -> None:
    from app.comfy.client import _parse_progress

    raw = json.dumps({"type": "status",
                      "data": {"status": {"exec_info": {"queue_remaining": 4}}}})
    assert _parse_progress(raw, "p1").queue_remaining == 4
