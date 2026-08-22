# Architecture decisions

## D1 — Workflow templates are captured from execution history, not authored
**Date**: 2026-08-22 · **Status**: accepted

ComfyUI's `/history` endpoint returns the exact API-format graph of every run, including
whether it succeeded. Capturing a template from a successful run gives a graph that is
*known* to execute on this machine with these models — stronger than a hand-written graph
or a UI export, both of which can drift from what actually ran.

`workflows/txt2img_h3_plate.api.json` was captured this way. UI exports remain a valid
fallback for workflows that have never been run.

## D2 — "Image generation" here means a 5-frame video clip
**Date**: 2026-08-22 · **Status**: accepted, pending owner confirmation

This machine has **no image checkpoints** — only the MiniMax H3 video stack. Stills are
produced by running H3 at its minimum clip length and saving the frames as PNGs, which is
how the existing `Rey_Temple_Plate` workflow works.

Consequences: generation takes minutes (46 GB of weights, 10 GB of VRAM); there is no
negative prompt, CFG, or sampler choice in this graph; a job yields several near-identical
frames rather than one image.

The alternative — download an SDXL or Flux checkpoint for a real txt2img path — is faster
per image but costs 7–12 GB of a disk that has 99 GB free. Deferred to the owner.

## D3 — Sequential single-worker queue
**Date**: 2026-08-22 · **Status**: accepted

One GPU with 10 GB against a model that already offloads heavily. Concurrent jobs would
thrash. The queue processes strictly one job at a time; concurrency is not revisited until
measured evidence says the GPU can take it.

## D4 — Long polling, no inbound network surface
**Date**: 2026-08-22 · **Status**: accepted

Long polling means the machine makes only outbound HTTPS connections. No webhook, public
URL, tunnel, reverse proxy, or forwarded port — nothing for an outsider to reach. ComfyUI
stays on `127.0.0.1`. Revisit only if throughput ever demands it, which for a handful of
trusted users it will not.
