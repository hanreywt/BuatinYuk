"""Approved workflow templates and their editable parameters.

The security model: a graph is never generated from scratch and never edited by a
language model. Each workflow ships as a pair of files in the workflow directory:

    <id>.api.json    the ComfyUI API-format graph, treated as opaque
    <id>.meta.json   which node inputs may be substituted, and within what bounds

Only inputs declared in the metadata are ever written. Everything else in the graph -
model names, loader nodes, wiring, filename fields - is beyond reach of any request.
"""

from __future__ import annotations

import copy
import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.utils.logging import get_logger

log = get_logger(__name__)


class WorkflowError(Exception):
    """Base class for workflow problems. Safe message for users."""

    user_message = "That workflow is unavailable."


class WorkflowNotFound(WorkflowError):
    user_message = "That workflow does not exist."


class WorkflowDefinitionError(WorkflowError):
    """The template files on disk are inconsistent. An operator problem, not a user one."""

    user_message = "That workflow is misconfigured and has been disabled."


class ParameterError(WorkflowError):
    """A supplied parameter was rejected. `user_message` explains what to fix."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


_ALLOWED_TYPES = {"string", "int", "float"}


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One editable input, resolved to a concrete node and field in the graph."""

    name: str
    node: str
    input: str
    type: str
    required: bool = False
    managed: bool = False  # set by the orchestrator only; never accepted from a request
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    max_length: int | None = None
    note: str | None = None

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> ParameterSpec:
        kind = raw.get("type")
        if kind not in _ALLOWED_TYPES:
            raise WorkflowDefinitionError(
                f"parameter {name!r} has unsupported type {kind!r}; "
                f"expected one of {sorted(_ALLOWED_TYPES)}"
            )
        for required_key in ("node", "input"):
            if not raw.get(required_key):
                raise WorkflowDefinitionError(f"parameter {name!r} is missing {required_key!r}")

        return cls(
            name=name,
            node=str(raw["node"]),
            input=str(raw["input"]),
            type=kind,
            required=bool(raw.get("required", False)),
            managed=bool(raw.get("managed", False)),
            default=raw.get("default"),
            minimum=raw.get("min"),
            maximum=raw.get("max"),
            step=raw.get("step"),
            max_length=raw.get("max_length"),
            note=raw.get("note"),
        )

    def describe(self) -> str:
        bits = [self.type]
        if self.minimum is not None or self.maximum is not None:
            bits.append(f"{_fmt(self.minimum)}..{_fmt(self.maximum)}")
        if self.step:
            bits.append(f"step {_fmt(self.step)}")
        if self.default is not None:
            bits.append(f"default {self.default}")
        return f"{self.name} ({', '.join(bits)})"


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """An approved workflow: its metadata plus the graph it drives."""

    workflow_id: str
    display_name: str
    description: str
    task: str
    output_type: str
    resource_intensity: str
    parameters: dict[str, ParameterSpec]
    unsupported: dict[str, str] = field(default_factory=dict)
    _graph: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def user_parameters(self) -> dict[str, ParameterSpec]:
        """Parameters a request may set. Managed ones are excluded by construction."""
        return {n: p for n, p in self.parameters.items() if not p.managed}

    def build(
        self, params: dict[str, Any], *, managed: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return a submittable graph with validated values substituted in.

        `params` is untrusted. `managed` carries orchestrator-controlled values such as
        the output filename prefix, and may set parameters marked `managed`.
        """
        unknown = set(params) - set(self.user_parameters)
        if unknown:
            offered = ", ".join(sorted(self.user_parameters))
            raise ParameterError(
                f"unknown parameters for {self.workflow_id}: {sorted(unknown)}",
                user_message=f"Unsupported setting: {sorted(unknown)[0]}. Available: {offered}.",
            )

        missing = [
            name
            for name, spec in self.user_parameters.items()
            if spec.required and name not in params
        ]
        if missing:
            raise ParameterError(
                f"missing required parameters: {missing}",
                user_message=f"Please provide: {', '.join(missing)}.",
            )

        graph = copy.deepcopy(self._graph)
        resolved: dict[str, Any] = {}

        # Apply declared defaults first. Without this the value baked into the captured
        # graph wins, which is rarely what the metadata says it should be - the txt2img
        # template shipped with length=73 from the run it was captured from, while the
        # metadata declares 5.
        for name, spec in self.user_parameters.items():
            if name not in params and spec.default is not None:
                resolved[name] = _coerce(spec, spec.default)

        for name, raw_value in params.items():
            spec = self.parameters[name]
            resolved[name] = _coerce(spec, raw_value)

        for name, raw_value in (managed or {}).items():
            spec = self.parameters.get(name)
            if spec is None:
                raise WorkflowDefinitionError(
                    f"{self.workflow_id} has no parameter {name!r} to manage"
                )
            resolved[name] = _coerce(spec, raw_value)

        for name, value in resolved.items():
            spec = self.parameters[name]
            graph[spec.node]["inputs"][spec.input] = value

        log.debug(
            "workflow.built",
            workflow=self.workflow_id,
            applied=sorted(resolved),
            nodes=len(graph),
        )
        return graph


class WorkflowRegistry:
    """Loads and serves the approved workflows. Read-only after construction."""

    def __init__(self, workflows: dict[str, WorkflowSpec]) -> None:
        self._workflows = workflows

    @classmethod
    def load(cls, directory: Path, *, strict: bool = False) -> WorkflowRegistry:
        """Load every `<id>.meta.json` in `directory`.

        A broken template is skipped with a loud log rather than taking the whole
        application down, unless `strict` is set (used by tests and by the audit).
        """
        workflows: dict[str, WorkflowSpec] = {}
        for meta_path in sorted(directory.glob("*.meta.json")):
            try:
                spec = _load_workflow(meta_path)
            except WorkflowDefinitionError as exc:
                log.error("workflow.invalid", file=meta_path.name, error=str(exc))
                if strict:
                    raise
                continue
            if spec.workflow_id in workflows:
                message = f"duplicate workflow id {spec.workflow_id!r} in {meta_path.name}"
                log.error("workflow.duplicate", file=meta_path.name, error=message)
                if strict:
                    raise WorkflowDefinitionError(message)
                continue
            workflows[spec.workflow_id] = spec

        log.info("workflow.registry.loaded", count=len(workflows), ids=sorted(workflows))
        return cls(workflows)

    def get(self, workflow_id: str) -> WorkflowSpec:
        try:
            return self._workflows[workflow_id]
        except KeyError:
            raise WorkflowNotFound(f"no approved workflow with id {workflow_id!r}") from None

    def list(self) -> list[WorkflowSpec]:
        return sorted(self._workflows.values(), key=lambda w: w.workflow_id)

    def ids(self) -> list[str]:
        return sorted(self._workflows)

    def __contains__(self, workflow_id: object) -> bool:
        return workflow_id in self._workflows

    def __len__(self) -> int:
        return len(self._workflows)


# ---------------- loading ----------------


def _load_workflow(meta_path: Path) -> WorkflowSpec:
    meta = _read_json(meta_path)

    workflow_id = meta.get("workflow_id") or meta_path.name.removesuffix(".meta.json")
    graph_name = meta.get("graph_file") or f"{workflow_id}.api.json"
    graph_path = meta_path.parent / graph_name
    if not graph_path.is_file():
        raise WorkflowDefinitionError(f"{workflow_id}: graph file {graph_name!r} not found")

    graph = _read_json(graph_path)
    _validate_graph_shape(workflow_id, graph)

    parameters = {
        name: ParameterSpec.from_dict(name, raw)
        for name, raw in (meta.get("parameters") or {}).items()
    }
    if not parameters:
        raise WorkflowDefinitionError(f"{workflow_id}: declares no editable parameters")

    # Every mapping must point at something that actually exists. This is what catches
    # a workflow re-export that renamed or renumbered a node.
    for spec in parameters.values():
        node = graph.get(spec.node)
        if node is None:
            raise WorkflowDefinitionError(
                f"{workflow_id}: parameter {spec.name!r} targets node {spec.node!r}, "
                "which is not in the graph"
            )
        if spec.input not in (node.get("inputs") or {}):
            raise WorkflowDefinitionError(
                f"{workflow_id}: parameter {spec.name!r} targets input {spec.input!r} on "
                f"node {spec.node} ({node.get('class_type')}), which has no such input"
            )

    return WorkflowSpec(
        workflow_id=workflow_id,
        display_name=meta.get("display_name") or workflow_id,
        description=meta.get("description") or "",
        task=meta.get("task") or "unknown",
        output_type=meta.get("output_type") or "image",
        resource_intensity=meta.get("resource_intensity") or "unknown",
        parameters=parameters,
        unsupported=meta.get("unsupported") or {},
        _graph=graph,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowDefinitionError(f"{path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowDefinitionError(f"{path.name}: expected a JSON object")
    return data


def _validate_graph_shape(workflow_id: str, graph: dict[str, Any]) -> None:
    """Confirm this is an API-format graph, not a UI export.

    UI exports have top-level "nodes"/"links" keys and cannot be POSTed to /prompt.
    Catching that here gives a clear message instead of an opaque ComfyUI rejection.
    """
    if "nodes" in graph and "links" in graph:
        raise WorkflowDefinitionError(
            f"{workflow_id}: this is a UI-format workflow. Re-export it with "
            "Workflow -> Export (API), or capture it from ComfyUI's history."
        )
    if not graph:
        raise WorkflowDefinitionError(f"{workflow_id}: graph is empty")
    for node_id, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node:
            raise WorkflowDefinitionError(
                f"{workflow_id}: node {node_id!r} is not an API-format node"
            )
        if not isinstance(node.get("inputs"), dict):
            raise WorkflowDefinitionError(f"{workflow_id}: node {node_id!r} has no inputs object")


# ---------------- coercion ----------------


def _coerce(spec: ParameterSpec, value: Any) -> Any:
    if spec.type == "string":
        return _coerce_string(spec, value)
    return _coerce_number(spec, value)


def _coerce_string(spec: ParameterSpec, value: Any) -> str:
    if not isinstance(value, str):
        raise ParameterError(
            f"{spec.name} must be text, got {type(value).__name__}",
            user_message=f"{spec.name} must be text.",
        )

    # Strip control characters; keep newlines and tabs, which prompts legitimately use.
    cleaned = "".join(
        ch for ch in value if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
    ).strip()

    if spec.required and not cleaned:
        raise ParameterError(
            f"{spec.name} is empty", user_message=f"Please provide a {spec.name}."
        )
    if spec.max_length is not None and len(cleaned) > spec.max_length:
        raise ParameterError(
            f"{spec.name} is {len(cleaned)} characters, limit {spec.max_length}",
            user_message=f"Your {spec.name} is too long (limit {spec.max_length} characters).",
        )
    return cleaned


def _coerce_number(spec: ParameterSpec, value: Any) -> int | float:
    if isinstance(value, bool):  # bool is an int subclass; never meant numerically here
        raise ParameterError(
            f"{spec.name} must be a number", user_message=f"{spec.name} must be a number."
        )
    try:
        number = int(value) if spec.type == "int" else float(value)
    except (TypeError, ValueError):
        raise ParameterError(
            f"{spec.name} must be {spec.type}, got {value!r}",
            user_message=f"{spec.name} must be a number.",
        ) from None

    original = number
    if spec.minimum is not None:
        number = max(number, spec.minimum)
    if spec.maximum is not None:
        number = min(number, spec.maximum)

    # Snap onto the node's accepted grid so ComfyUI never sees an off-grid value.
    if spec.step:
        base = spec.minimum if spec.minimum is not None else 0
        offset = round((number - base) / spec.step) * spec.step
        number = base + offset
        if spec.maximum is not None and number > spec.maximum:
            number -= spec.step
        if spec.minimum is not None and number < spec.minimum:
            number += spec.step

    number = int(round(number)) if spec.type == "int" else float(number)
    if number != original:
        log.debug("workflow.value.adjusted", parameter=spec.name, given=original, used=number)
    return number


def _fmt(value: float | None) -> str:
    if value is None:
        return "*"
    return str(int(value)) if float(value).is_integer() else str(value)
