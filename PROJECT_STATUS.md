# PROJECT STATUS

Living handover document. **Any Claude Code session picking this project up should read
this file first**, then run `python scripts/audit_env.py` to re-verify the machine.

Last updated: 2026-08-22 · Current version: **v0.1-dev (Phase 0 done, Phase 1 foundations done)**

---

## Phase checklist

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Environment audit, project skeleton, docs | **DONE** |
| 1 | Telegram -> ComfyUI MVP (single fixed workflow, single user) | **IN PROGRESS** |
| 2 | Persistent job system + sequential GPU queue (SQLite) | NOT STARTED |
| 3 | Multi-user, roles, invites, quotas, admin commands | NOT STARTED |
| 4 | Local MCP server (narrow tools) | NOT STARTED |
| 5 | Claude natural-language interpretation layer | NOT STARTED |
| 6 | Video, upscale, inline buttons, retention | NOT STARTED |

---

## Discovered environment (verified 2026-08-22)

Re-run `python scripts/audit_env.py` to refresh. All values below were probed, not assumed.

### Host
- **OS**: Windows 11 Pro (build 26200; Python reports it as "Windows 10")
- **Python**: 3.11.9 at `C:\Users\user\AppData\Local\Programs\Python\Python311\python.exe`
- **Git**: 2.49.0.windows.1 — available. Project is **not yet a git repo** (not initialised; awaiting authorisation)
- **Claude Code**: 2.1.238
- **GPU**: NVIDIA GeForce RTX 3080, **10 GB VRAM**, driver 591.86
- **Disk**: `D:` 99 GB free (90% used) · `C:` 26 GB free (95% used) — **watch this**
- **Shell**: PowerShell primary, Git Bash available

### ComfyUI
- **Type**: ComfyUI **Desktop** (Electron launcher), not a manual git clone
- **Version**: 0.33.2, torch 2.12.1+cu130, its own bundled Python 3.13.12
- **URL**: `http://127.0.0.1:8188` — **bound to loopback only** (no `--listen`). Good; keep it that way.
- **Install root**: `D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI`
- **Output dir**: `D:\Comfy-Desktop\ComfyUI-Shared\output`
- **Input dir**: `D:\Comfy-Desktop\ComfyUI-Shared\input`
- **Models root**: `D:\Comfy-Desktop\ComfyUI-Shared\models`
- **Saved UI workflows**: `...\ComfyUI\user\default\workflows\` (37 files, all `Rey_*`)
- **Manager**: enabled (`--enable-manager`)

### Installed models — IMPORTANT CONSTRAINT
There are **no image checkpoints installed at all**. The entire stack is one video model:

| Slot | File | Size |
|------|------|------|
| diffusion_models | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 20.0 GB |
| text_encoders | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | 25.9 GB |
| vae | `minimax_h3_video_vae_fp16.safetensors` | 5.0 GB |
| vae | `minimax_h3_audio_vae_fp32.safetensors` | 0.6 GB |
| loras | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 1.9 GB |
| frame_interpolation | `film_net_fp16.safetensors` | 0.07 GB |
| background_removal | `birefnet.safetensors` | 0.4 GB |

Empty: `checkpoints`, `controlnet`, `upscale_models`, `clip_vision`, `embeddings`, etc.

**Consequence**: "text to image" on this machine means *running the MiniMax H3 video model at
its shortest clip length and saving the frames as PNGs*. That is exactly how the existing
`Rey_Temple_Plate` workflow produces stills. There is no SD/SDXL/Flux fast path available.

**Consequence**: 46 GB of weights against 10 GB of VRAM means heavy offloading. Expect
generation times in **minutes, not seconds**, and expect the first request after an idle
period to be slower (model reload). Phase 1 timeouts must be generous.

### Known-good workflow (captured, verified)
`workflows/txt2img_h3_plate.api.json` — 13 nodes, extracted directly from ComfyUI's own
execution history (`prompt_id f813cb25-1be7-4a27-b89a-69c56eeb110d`, status `success`).
This is API-format JSON, ready to POST to `/prompt`. Its editable parameters are documented
in `workflows/txt2img_h3_plate.meta.json`.

Editable: `prompt`, `width`, `height`, `length`, `seed`, `steps`, `filename_prefix`.
Not available in this graph: negative prompt, CFG scale, sampler choice, model choice.

The original run used `length=73` (73 PNGs, 608x1088). For single-image jobs use `length=5`
(the node's minimum; step grid is 17).

---

## Phase 1 progress

Built and verified (88 tests passing, `python -m pytest`):

| Component | File | State |
|---|---|---|
| Typed configuration | `app/config/settings.py` | Done. Secrets in `SecretStr`, `__repr__` hides the token, relative paths resolve against the project root, non-loopback `COMFYUI_HOST` is refused. |
| Path safety | `app/utils/paths.py` | Done. `sanitize_name` / `safe_join` / `assert_within`. Traversal, separators, NUL bytes, and Windows reserved device names all neutralised. |
| Structured logging | `app/utils/logging.py` | Done. Console + rotating JSON file (5 MB x 5). Central redaction of secret-shaped keys and of anything matching a bot-token pattern. |
| ComfyUI client | `app/comfy/client.py` | Done. Verified against the live instance: status, submit, history polling, WS progress, streamed download. |
| ComfyUI error taxonomy | `app/comfy/errors.py` | Done. Each error carries a safe `user_message`; detail stays in logs. |
| Workflow registry | `app/workflows/registry.py` | Done. Loads `<id>.api.json` + `<id>.meta.json`, validates every mapping against the real graph, coerces/clamps/snaps values. |

Still to build for v0.1: SQLite job store, the sequential queue worker, the orchestrator,
and the Telegram bot itself.

### Design points worth knowing
- **Completion is decided by polling `/history`**, not by the WebSocket. The socket only
  feeds progress updates and its failure is non-fatal. A ComfyUI restart mid-job is
  tolerated for ~5 poll cycles before the job is failed.
- **`filename_prefix` is a `managed` parameter** — declared in the workflow metadata but
  excluded from `user_parameters`, so a request cannot reach it. Only the orchestrator
  passes it, via `build(..., managed={...})`.
- **Out-of-range numbers are clamped and snapped to the node's grid** rather than
  rejected, so ComfyUI never receives an off-grid value. Wrong *types*, unknown
  parameters, and overlong strings are rejected outright.
- **Client is verified against the real instance**: downloaded an actual 824 KB PNG
  through `/view`, and confirmed a dead port yields `ComfyUnavailable` with the safe
  message "The image generator is offline right now."

---

## Open questions — BLOCKING Phase 1

1. **Telegram bot token** — not yet created. Needed in `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Owner's numeric Telegram user ID** — needed as `ADMIN_TELEGRAM_IDS`. From `@userinfobot`.
3. ~~Image strategy~~ — **DECIDED 2026-08-22**: use the H3 stack as-is. An SDXL/Flux
   workflow can be added later as another template; the registry supports it with no
   code change.
4. **Unverified**: no run at `length=5` has been performed. The 5-frame path is inferred
   from the node schema (`min=5, step=17`), not observed. **Verify this before relying on
   it** — confirm how many PNGs come back and how long it takes.

---

## Security posture (current)
- ComfyUI on loopback only — verified via `argv`, no `--listen` flag.
- No secrets exist yet. `.env.example` holds placeholders only; `.env` is gitignored.
- No public tunnel, webhook, or port forward. Telegram will use **long polling**.
- Nothing has been committed to git (repo not initialised).

---

## Next concrete action
1. Owner creates the bot with @BotFather and fills `.env` (items 1 and 2 above).
2. Verify the `length=5` path with one real generation (item 4) and record the timing here.
3. Build the SQLite job store + sequential queue worker, then the orchestrator, then the
   Telegram bot. Finish with the end-to-end test in `docs/`.

## Deliberately NOT done yet
Docker, Redis, Celery, PostgreSQL, cloud anything, MCP server, Claude routing, inline
buttons, video jobs, retention/cleanup. All are later phases or explicitly out of scope.
