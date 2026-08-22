"""Environment audit for the Telegram -> ComfyUI generation server.

Read-only. Probes the local machine and the local ComfyUI instance and prints a
report. Run this after any ComfyUI upgrade, model change, or on a fresh machine.

    python scripts/audit_env.py
    python scripts/audit_env.py --json
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_COMFY = "http://127.0.0.1:8188"
TIMEOUT = 10


def _get(base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base}{path}", timeout=TIMEOUT) as r:
        return json.load(r)


def probe_host() -> dict[str, Any]:
    info: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "git": _tool_version(["git", "--version"]),
        "claude_code": _tool_version(["claude", "--version"]),
    }
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=TIMEOUT, check=True,
        ).stdout.strip()
        info["gpu"] = out or None
    except Exception:
        info["gpu"] = None
    return info


def _tool_version(cmd: list[str]) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT).stdout.strip() or None
    except Exception:
        return None


def probe_comfy(base: str) -> dict[str, Any]:
    result: dict[str, Any] = {"base_url": base, "reachable": False}
    try:
        stats = _get(base, "/system_stats")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["reachable"] = True
    system = stats.get("system", {})
    result["comfyui_version"] = system.get("comfyui_version")
    result["python_version"] = system.get("python_version")
    result["pytorch_version"] = system.get("pytorch_version")

    argv = system.get("argv") or []
    result["argv"] = argv
    result["output_directory"] = _argv_value(argv, "--output-directory")
    result["input_directory"] = _argv_value(argv, "--input-directory")
    result["listen_flag"] = _argv_value(argv, "--listen")

    result["devices"] = [
        {"name": d.get("name"), "type": d.get("type"),
         "vram_total_gb": round((d.get("vram_total") or 0) / 2**30, 2),
         "vram_free_gb": round((d.get("vram_free") or 0) / 2**30, 2)}
        for d in stats.get("devices", [])
    ]

    try:
        queue = _get(base, "/queue")
        result["queue_running"] = len(queue.get("queue_running", []))
        result["queue_pending"] = len(queue.get("queue_pending", []))
    except Exception as exc:
        result["queue_error"] = str(exc)

    result["models"] = probe_models(base)
    return result


def _argv_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


MODEL_PROBES = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "text_encoders": ("CLIPLoader", "clip_name"),
    "vae": ("VAELoader", "vae_name"),
    "loras": ("LoraLoaderModelOnly", "lora_name"),
    "upscale_models": ("UpscaleModelLoader", "model_name"),
}


def probe_models(base: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for label, (node, field) in MODEL_PROBES.items():
        try:
            info = _get(base, f"/object_info/{node}")
            spec = info[node]["input"]["required"][field][0]
            found[label] = list(spec) if isinstance(spec, list) else []
        except Exception:
            found[label] = []
    return found


def probe_workflows(root: Path) -> dict[str, Any]:
    wf_dir = root / "workflows"
    graphs = sorted(p.name for p in wf_dir.glob("*.api.json"))
    metas = sorted(p.name for p in wf_dir.glob("*.meta.json"))
    return {"dir": str(wf_dir), "graphs": graphs, "metadata": metas}


def probe_secrets(root: Path) -> dict[str, Any]:
    env = root / ".env"
    return {
        "env_present": env.exists(),
        "env_example_present": (root / ".env.example").exists(),
        "gitignored": ".env" in (root / ".gitignore").read_text(encoding="utf-8")
        if (root / ".gitignore").exists() else False,
    }


def render(report: dict[str, Any]) -> str:
    lines: list[str] = ["=" * 62, "ENVIRONMENT AUDIT", "=" * 62, "", "[host]"]
    h = report["host"]
    lines += [
        f"  OS               : {h['os']}",
        f"  Python           : {h['python']} ({'OK' if h['python_ok'] else 'NEEDS 3.11+'})",
        f"  Git              : {h['git'] or 'NOT FOUND'}",
        f"  Claude Code      : {h['claude_code'] or 'NOT FOUND'}",
        f"  GPU              : {h['gpu'] or 'NOT DETECTED'}",
        "", "[comfyui]",
    ]
    c = report["comfyui"]
    if not c["reachable"]:
        lines += [f"  UNREACHABLE at {c['base_url']}", f"  {c.get('error', '')}",
                  "  -> Start ComfyUI, then re-run this audit."]
    else:
        lines += [
            f"  URL              : {c['base_url']}",
            f"  Version          : {c['comfyui_version']}  (torch {c['pytorch_version']})",
            f"  Output dir       : {c['output_directory'] or '(default)'}",
            f"  Bound publicly   : {'YES - REVIEW THIS' if c['listen_flag'] not in (None, '127.0.0.1') else 'no (localhost only)'}",
            f"  Queue            : {c.get('queue_running', '?')} running / {c.get('queue_pending', '?')} pending",
        ]
        for d in c["devices"]:
            lines.append(f"  Device           : {d['name']} "
                         f"({d['vram_free_gb']}/{d['vram_total_gb']} GB VRAM free)")
        lines.append("  Models:")
        for label, items in c["models"].items():
            shown = ", ".join(items) if items else "(none)"
            lines.append(f"    {label:<18}: {shown}")

    w = report["workflows"]
    lines += ["", "[workflows]",
              f"  Graphs           : {', '.join(w['graphs']) or '(none)'}",
              f"  Metadata         : {', '.join(w['metadata']) or '(none)'}"]
    s = report["secrets"]
    lines += ["", "[secrets]",
              f"  .env present     : {s['env_present']}",
              f"  .env.example     : {s['env_example_present']}",
              f"  .env gitignored  : {s['gitignored']}", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--comfy-url", default=DEFAULT_COMFY)
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    report = {
        "host": probe_host(),
        "comfyui": probe_comfy(args.comfy_url),
        "workflows": probe_workflows(root),
        "secrets": probe_secrets(root),
    }
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["comfyui"]["reachable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
