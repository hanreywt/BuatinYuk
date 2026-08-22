"""Video routing, media-kind detection, and the shipped video templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bot.handlers import _strip_video_prefix
from app.comfy.models import OutputRef, kind_for_suffix, OutputKind
from app.workflows.registry import WorkflowRegistry


# ---------------- asking for video ----------------


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("/video make it move", (True, "make it move")),
        ("/VIDEO loud", (True, "loud")),
        ("/video@somebot pan", (True, "pan")),
        ("video: spin around", (True, "spin around")),
        ("animate: zoom in", (True, "zoom in")),
        ("  /video  slow pan  ", (True, "slow pan")),
        ("/video", (True, "")),
    ],
)
def test_a_caption_can_ask_for_video(caption: str, expected: tuple[bool, str]) -> None:
    assert _strip_video_prefix(caption) == expected


@pytest.mark.parametrize(
    "caption",
    ["make this cinematic", "/videos of cats", "videoclip of a dog", "a video game screenshot", ""],
)
def test_a_description_is_not_mistaken_for_a_command(caption: str) -> None:
    """"/videos of cats" is a description; treating it as a command would eat the "s"."""
    assert _strip_video_prefix(caption) == (False, caption)


# ---------------- media kind ----------------


@pytest.mark.parametrize(
    ("suffix", "kind"),
    [
        (".mp4", OutputKind.VIDEO), (".webm", OutputKind.VIDEO), (".mov", OutputKind.VIDEO),
        (".png", OutputKind.IMAGE), (".jpg", OutputKind.IMAGE),
        (".mp3", OutputKind.AUDIO), (".wav", OutputKind.AUDIO),
    ],
)
def test_kind_comes_from_the_extension(suffix: str, kind: OutputKind) -> None:
    assert kind_for_suffix(suffix) is kind


def test_extension_outranks_comfyuis_own_grouping() -> None:
    """SaveVideo reports its .mp4 under the "images" key, so the key cannot be trusted."""
    ref = OutputRef("clip_00001_.mp4", "", "output", kind=OutputKind.IMAGE)
    assert ref.kind is OutputKind.IMAGE       # what ComfyUI said
    assert ref.actual_kind is OutputKind.VIDEO  # what it actually is


# ---------------- shipped templates ----------------


@pytest.fixture(scope="module")
def registry(request) -> WorkflowRegistry:
    return WorkflowRegistry.load(Path(__file__).resolve().parent.parent / "workflows", strict=True)


def test_all_four_workflows_load(registry: WorkflowRegistry) -> None:
    assert set(registry.ids()) == {
        "txt2img_h3_plate", "img2img_h3", "txt2video_h3", "img2video_h3",
    }


@pytest.mark.parametrize("workflow_id", ["txt2video_h3", "img2video_h3"])
def test_video_workflows_declare_video_output(registry: WorkflowRegistry, workflow_id: str) -> None:
    assert registry.get(workflow_id).output_type == "video"


@pytest.mark.parametrize("workflow_id", ["txt2video_h3", "img2video_h3"])
def test_video_graphs_keep_the_audio_branch(registry: WorkflowRegistry, workflow_id: str) -> None:
    """H3 produces sound; dropping the audio chain would silently lose it."""
    graph = registry.get(workflow_id)._graph
    classes = {node["class_type"] for node in graph.values()}
    assert "VAEDecodeAudio" in classes
    assert "CreateVideo" in classes and "SaveVideo" in classes
    create_video = next(n for n in graph.values() if n["class_type"] == "CreateVideo")
    assert "audio" in create_video["inputs"]


def test_txt2video_has_no_input_image_branch(registry: WorkflowRegistry) -> None:
    graph = registry.get("txt2video_h3")._graph
    classes = {node["class_type"] for node in graph.values()}
    assert "LoadImage" not in classes
    node = next(n for n in graph.values() if n["class_type"] == "MiniMaxH3ImageToVideo")
    assert "first_frame" not in node["inputs"]


def test_img2video_keeps_the_input_image_branch(registry: WorkflowRegistry) -> None:
    graph = registry.get("img2video_h3")._graph
    node = next(n for n in graph.values() if n["class_type"] == "MiniMaxH3ImageToVideo")
    assert "first_frame" in node["inputs"]
    assert "image" in registry.get("img2video_h3").parameters


def test_img2video_size_drives_both_generation_and_the_scaler(registry: WorkflowRegistry) -> None:
    """If these drift apart the input image no longer matches the generation size."""
    built = registry.get("img2video_h3").build(
        {"prompt": "x", "width": 640, "height": 640},
        managed={"filename_prefix": "p", "image": "in.png"},
    )
    assert built["8"]["inputs"]["width"] == 640   # the generation
    assert built["2"]["inputs"]["width"] == 640   # the input scaler
    assert built["8"]["inputs"]["height"] == 640
    assert built["2"]["inputs"]["height"] == 640


@pytest.mark.parametrize("workflow_id", ["txt2video_h3", "img2video_h3"])
def test_video_length_stays_on_the_models_frame_grid(
    registry: WorkflowRegistry, workflow_id: str
) -> None:
    workflow = registry.get(workflow_id)
    managed = {"filename_prefix": "p"}
    if "image" in workflow.parameters:
        managed["image"] = "in.png"
    for given in (1, 5, 40, 56, 999):
        built = workflow.build({"prompt": "x", "length": given}, managed=managed)
        value = built["8"]["inputs"]["length"]
        assert 5 <= value <= 124 and (value - 5) % 17 == 0


def test_video_default_matches_the_verified_run(registry: WorkflowRegistry) -> None:
    built = registry.get("txt2video_h3").build({"prompt": "x"}, managed={"filename_prefix": "p"})
    assert built["8"]["inputs"]["length"] == 56
    assert built["11"]["inputs"]["steps"] == 8  # the turbo LoRA is tuned for 8
