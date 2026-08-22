"""End-to-end generation check: workflow registry -> ComfyUI -> file on disk.

This is the Telegram-free half of the integration test. It exercises the same code
paths the bot will use - load an approved workflow, validate parameters, submit, watch
progress, download - so a failure here is a real failure, not a mock artefact.

    python scripts/verify_generation.py
    python scripts/verify_generation.py --prompt "a red bicycle" --length 5 --steps 12

It needs no .env: ComfyUI's address can be passed on the command line, and nothing
Telegram-related is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.comfy.client import ComfyUIClient  # noqa: E402
from app.comfy.errors import ComfyError  # noqa: E402
from app.comfy.models import Progress  # noqa: E402
from app.utils.logging import configure_logging  # noqa: E402
from app.utils.paths import safe_join  # noqa: E402
from app.workflows.registry import WorkflowError, WorkflowRegistry  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def run(args: argparse.Namespace) -> int:
    registry = WorkflowRegistry.load(PROJECT_ROOT / "workflows", strict=True)
    workflow = registry.get(args.workflow)

    print(f"workflow   : {workflow.workflow_id} ({workflow.display_name})")
    print(f"parameters : {', '.join(workflow.user_parameters)}")

    params = {"prompt": args.prompt, "length": args.length, "steps": args.steps}
    if args.width:
        params["width"] = args.width
    if args.height:
        params["height"] = args.height
    if args.seed is not None:
        params["seed"] = args.seed

    prefix = f"verify_{int(time.time())}"
    graph = workflow.build(params, managed={"filename_prefix": prefix})
    applied = graph["1"]["inputs"]
    print(f"submitting : {applied['width']}x{applied['height']}, "
          f"length={applied['length']}, steps={graph['9']['inputs']['steps']}")

    base = f"http://{args.host}:{args.port}"
    async with ComfyUIClient(base, f"ws://{args.host}:{args.port}/ws") as client:
        status = await client.status()
        if not status.online:
            print(f"FAIL: ComfyUI is not reachable at {base}")
            return 1
        print(f"comfyui    : {status.version}, {status.vram_free_gb} GB VRAM free")

        started = time.monotonic()
        last_line = ""

        async def on_progress(progress: Progress) -> None:
            nonlocal last_line
            if progress.percent is not None:
                line = f"  step {progress.step}/{progress.total_steps} ({progress.percent}%)"
            elif progress.node is not None:
                line = f"  executing node {progress.node}"
            else:
                return
            if line != last_line:
                last_line = line
                print(f"{line}   (t+{time.monotonic() - started:.0f}s)", flush=True)

        prompt_id = await client.submit(graph)
        print(f"prompt_id  : {prompt_id}")

        outputs = await client.wait(prompt_id, timeout=args.timeout, on_progress=on_progress)
        elapsed = time.monotonic() - started

        destination_dir = PROJECT_ROOT / "outputs" / "verify"
        saved = []
        for index, ref in enumerate(outputs, start=1):
            suffix = Path(ref.filename).suffix or ".bin"
            target = safe_join(destination_dir, f"{prefix}_{index:03d}{suffix}")
            saved.append(await client.download(ref, target))

    print()
    print(f"RESULT     : {len(outputs)} output(s) in {elapsed:.0f}s "
          f"({elapsed / max(len(outputs), 1):.0f}s each)")
    for path in saved[: args.show]:
        print(f"  {path.relative_to(PROJECT_ROOT)}  ({path.stat().st_size / 1024:.0f} KB)")
    if len(saved) > args.show:
        print(f"  ... and {len(saved) - args.show} more in {destination_dir.name}/")
    print()
    print("PASS: registry -> ComfyUI -> local file works end to end.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", default="txt2img_h3_plate")
    parser.add_argument("--prompt", default="a cinematic photograph of futuristic Jakarta "
                                            "at night, neon reflections on wet streets")
    parser.add_argument("--length", type=int, default=5, help="frame count (5 = fewest)")
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--show", type=int, default=5, help="how many saved paths to print")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    configure_logging(PROJECT_ROOT / "logs", args.log_level)

    try:
        return asyncio.run(run(args))
    except (ComfyError, WorkflowError) as exc:
        # The operator gets the real detail here; a Telegram user would get user_message.
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        print(f"      (a bot user would see: {exc.user_message!r})")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
