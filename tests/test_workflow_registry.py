"""Workflow loading and parameter validation - the gate between a request and the GPU."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.workflows.registry import (
    ParameterError,
    WorkflowDefinitionError,
    WorkflowNotFound,
    WorkflowRegistry,
)


@pytest.fixture
def registry(workflow_dir: Path) -> WorkflowRegistry:
    return WorkflowRegistry.load(workflow_dir, strict=True)


# ---------------- loading ----------------


def test_loads_declared_workflow(registry: WorkflowRegistry) -> None:
    assert registry.ids() == ["test_wf"]
    assert "test_wf" in registry
    assert len(registry) == 1
    assert registry.get("test_wf").display_name == "Test Workflow"


def test_unknown_workflow_raises_with_a_safe_message(registry: WorkflowRegistry) -> None:
    with pytest.raises(WorkflowNotFound) as exc:
        registry.get("does_not_exist")
    assert "does not exist" in exc.value.user_message


def test_ui_format_graph_is_rejected(write_workflow, graph: dict, meta: dict) -> None:
    """A UI export cannot be POSTed to /prompt; catch it at load time."""
    ui_graph = {"nodes": [], "links": [], "last_node_id": 3}
    directory = write_workflow("ui_wf", ui_graph, {**meta, "workflow_id": "ui_wf"})
    with pytest.raises(WorkflowDefinitionError, match="Export \\(API\\)"):
        WorkflowRegistry.load(directory, strict=True)


def test_parameter_pointing_at_a_missing_node_is_rejected(
    write_workflow, graph: dict, meta: dict
) -> None:
    """Catches a re-exported workflow whose node ids changed."""
    meta["parameters"]["prompt"]["node"] = "99"
    directory = write_workflow("test_wf", graph, meta)
    with pytest.raises(WorkflowDefinitionError, match="not in the graph"):
        WorkflowRegistry.load(directory, strict=True)


def test_parameter_pointing_at_a_missing_input_is_rejected(
    write_workflow, graph: dict, meta: dict
) -> None:
    meta["parameters"]["prompt"]["input"] = "no_such_input"
    directory = write_workflow("test_wf", graph, meta)
    with pytest.raises(WorkflowDefinitionError, match="no such input"):
        WorkflowRegistry.load(directory, strict=True)


def test_missing_graph_file_is_rejected(tmp_path: Path, meta: dict) -> None:
    (tmp_path / "test_wf.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(WorkflowDefinitionError, match="not found"):
        WorkflowRegistry.load(tmp_path, strict=True)


def test_broken_workflow_is_skipped_when_not_strict(
    write_workflow, graph: dict, meta: dict
) -> None:
    """One bad template must not take the whole application down at startup."""
    meta["parameters"]["prompt"]["node"] = "99"
    directory = write_workflow("test_wf", graph, meta)
    assert len(WorkflowRegistry.load(directory, strict=False)) == 0


def test_empty_directory_loads_cleanly(tmp_path: Path) -> None:
    assert len(WorkflowRegistry.load(tmp_path, strict=True)) == 0


# ---------------- building ----------------


def test_build_substitutes_only_mapped_inputs(registry: WorkflowRegistry) -> None:
    built = registry.get("test_wf").build({"prompt": "a city", "width": 512})
    assert built["1"]["inputs"]["prompt"] == "a city"
    assert built["1"]["inputs"]["width"] == 512
    # Anything unmapped is untouchable.
    assert built["2"]["inputs"]["model_name"] == "locked.safetensors"


def test_build_does_not_mutate_the_template(registry: WorkflowRegistry) -> None:
    workflow = registry.get("test_wf")
    workflow.build({"prompt": "first"})
    second = workflow.build({"prompt": "second"})
    assert second["1"]["inputs"]["prompt"] == "second"
    # A third build must not inherit anything from the earlier ones.
    assert workflow.build({"prompt": "third"})["1"]["inputs"]["width"] == 512


def test_unknown_parameter_is_rejected(registry: WorkflowRegistry) -> None:
    with pytest.raises(ParameterError, match="unknown parameters"):
        registry.get("test_wf").build({"prompt": "x", "cfg": 7})


def test_managed_parameter_cannot_be_set_by_a_request(registry: WorkflowRegistry) -> None:
    """The filename prefix is the orchestrator's; a user must never reach it."""
    with pytest.raises(ParameterError):
        registry.get("test_wf").build({"prompt": "x", "filename_prefix": "../../.env"})


def test_managed_parameter_is_settable_by_the_orchestrator(registry: WorkflowRegistry) -> None:
    built = registry.get("test_wf").build(
        {"prompt": "x"}, managed={"filename_prefix": "job_7_user_42"}
    )
    assert built["3"]["inputs"]["filename_prefix"] == "job_7_user_42"


def test_missing_required_parameter_is_rejected(registry: WorkflowRegistry) -> None:
    with pytest.raises(ParameterError, match="missing required"):
        registry.get("test_wf").build({"width": 512})


def test_user_parameters_exclude_managed_ones(registry: WorkflowRegistry) -> None:
    assert "filename_prefix" not in registry.get("test_wf").user_parameters
    assert "prompt" in registry.get("test_wf").user_parameters


# ---------------- coercion ----------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [(10, 256), (999999, 1024), (500, 512), (256, 256), (1024, 1024), ("512", 512), (513.0, 512)],
)
def test_numbers_are_clamped_and_snapped_to_the_node_grid(
    registry: WorkflowRegistry, given: object, expected: int
) -> None:
    built = registry.get("test_wf").build({"prompt": "x", "width": given})
    assert built["1"]["inputs"]["width"] == expected


@pytest.mark.parametrize("given", [1, 5, 6, 21, 22, 124, 9999])
def test_snapped_length_always_lands_on_the_models_frame_grid(
    registry: WorkflowRegistry, given: int
) -> None:
    """MiniMax H3 accepts 17k+5 frames; an off-grid value would be silently altered."""
    value = registry.get("test_wf").build({"prompt": "x", "length": given})["1"]["inputs"]["length"]
    assert 5 <= value <= 124
    assert (value - 5) % 17 == 0


@pytest.mark.parametrize("bad", ["abc", None, [], {}, True])
def test_non_numeric_values_are_rejected(registry: WorkflowRegistry, bad: object) -> None:
    with pytest.raises(ParameterError):
        registry.get("test_wf").build({"prompt": "x", "width": bad})


def test_overlong_string_is_rejected_not_truncated(registry: WorkflowRegistry) -> None:
    with pytest.raises(ParameterError, match="limit 100"):
        registry.get("test_wf").build({"prompt": "a" * 101})


def test_blank_required_string_is_rejected(registry: WorkflowRegistry) -> None:
    with pytest.raises(ParameterError, match="empty"):
        registry.get("test_wf").build({"prompt": "   \t  "})


def test_control_characters_are_stripped_from_strings(registry: WorkflowRegistry) -> None:
    built = registry.get("test_wf").build({"prompt": "hel\x00lo\x1b[31m wor\nld"})
    value = built["1"]["inputs"]["prompt"]
    assert "\x00" not in value and "\x1b" not in value
    assert "\n" in value  # newlines are legitimate in a prompt


def test_non_string_prompt_is_rejected(registry: WorkflowRegistry) -> None:
    with pytest.raises(ParameterError, match="must be text"):
        registry.get("test_wf").build({"prompt": 123})


# ---------------- the shipped template ----------------


def test_shipped_workflows_are_all_valid(real_workflow_dir: Path) -> None:
    """Guards the real templates in workflows/ against drift."""
    registry = WorkflowRegistry.load(real_workflow_dir, strict=True)
    assert "txt2img_h3_plate" in registry


def test_shipped_h3_workflow_builds_a_submittable_graph(real_workflow_dir: Path) -> None:
    workflow = WorkflowRegistry.load(real_workflow_dir, strict=True).get("txt2img_h3_plate")
    built = workflow.build(
        {"prompt": "futuristic Jakarta at night", "length": 5, "seed": 1},
        managed={"filename_prefix": "job_1_user_1"},
    )
    assert built["1"]["inputs"]["prompt"] == "futuristic Jakarta at night"
    assert built["1"]["inputs"]["length"] == 5
    assert built["7"]["inputs"]["noise_seed"] == 1
    assert built["13"]["inputs"]["filename_prefix"] == "job_1_user_1"
    # The model stack must survive untouched.
    assert built["4"]["inputs"]["unet_name"].startswith("minimax_h3")
    assert all("class_type" in node and "inputs" in node for node in built.values())
