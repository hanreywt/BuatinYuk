"""Shared fixtures.

Tests never touch Telegram, never touch ComfyUI, and never generate an image. The
ComfyUI client is exercised against an httpx mock transport, so its error mapping and
response parsing are tested without a GPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def real_workflow_dir() -> Path:
    """The project's actual workflow directory - guards the shipped templates."""
    return PROJECT_ROOT / "workflows"


@pytest.fixture
def graph() -> dict:
    """A minimal API-format graph shaped like the real one."""
    return {
        "1": {
            "class_type": "Sampler",
            "inputs": {"prompt": "", "width": 512, "height": 512, "length": 5},
        },
        "2": {"class_type": "Loader", "inputs": {"model_name": "locked.safetensors"}},
        "4": {"class_type": "RandomNoise", "inputs": {"noise_seed": 20250822}},
        "3": {"class_type": "SaveImage", "inputs": {"filename_prefix": "out", "images": ["1", 0]}},
    }


@pytest.fixture
def meta() -> dict:
    return {
        "workflow_id": "test_wf",
        "display_name": "Test Workflow",
        "task": "txt2img",
        "output_type": "image",
        "parameters": {
            "prompt": {"node": "1", "input": "prompt", "type": "string",
                       "required": True, "max_length": 100},
            "width": {"node": "1", "input": "width", "type": "int",
                      "default": 512, "min": 256, "max": 1024, "step": 64},
            "length": {"node": "1", "input": "length", "type": "int",
                       "min": 5, "max": 124, "step": 17},
            "seed": {"node": "4", "input": "noise_seed", "type": "int",
                     "min": 0, "max": 4294967295},
            "filename_prefix": {"node": "3", "input": "filename_prefix",
                                "type": "string", "managed": True},
        },
    }


@pytest.fixture
def workflow_dir(tmp_path: Path, graph: dict, meta: dict) -> Path:
    """An isolated workflow directory containing one valid workflow."""
    (tmp_path / "test_wf.api.json").write_text(json.dumps(graph), encoding="utf-8")
    (tmp_path / "test_wf.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return tmp_path


@pytest.fixture
def write_workflow(tmp_path: Path):
    """Write an arbitrary graph/meta pair, for testing rejection of bad definitions."""

    def _write(name: str, graph_data: dict, meta_data: dict) -> Path:
        (tmp_path / f"{name}.api.json").write_text(json.dumps(graph_data), encoding="utf-8")
        (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta_data), encoding="utf-8")
        return tmp_path

    return _write
