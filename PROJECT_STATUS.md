# PROJECT STATUS

Living handover document. **Any Claude Code session picking this project up should read
this file first**, then run `python scripts/audit_env.py` to re-verify the machine.

Last updated: 2026-08-22 · Current version: **v0.0 (Phase 0 complete)**

---

## Phase checklist

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Environment audit, project skeleton, docs | **DONE** |
| 1 | Telegram -> ComfyUI MVP (single fixed workflow, single user) | NOT STARTED |
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

## Open questions — BLOCKING Phase 1

1. **Telegram bot token** — not yet created. Needed in `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Owner's numeric Telegram user ID** — needed as `ADMIN_TELEGRAM_IDS`. From `@userinfobot`.
3. **Confirm the image strategy** — is "H3 at `length=5`, return frame 1" acceptable for v0.1,
   or should an SD/SDXL/Flux checkpoint be downloaded first for a genuinely fast txt2img path?
   (~7 GB for SDXL, ~12 GB for Flux dev; disk is tight.)
4. **Unverified**: no test run at `length=5` has been performed yet. The 5-frame path is
   inferred from the node schema (`min=5, step=17`), not observed. Verify before relying on it.

---

## Security posture (current)
- ComfyUI on loopback only — verified via `argv`, no `--listen` flag.
- No secrets exist yet. `.env.example` holds placeholders only; `.env` is gitignored.
- No public tunnel, webhook, or port forward. Telegram will use **long polling**.
- Nothing has been committed to git (repo not initialised).

---

## Next concrete action
Obtain the two Telegram values (items 1 and 2 above) and decide item 3. Then build Phase 1
in this order: typed config -> ComfyUI client -> workflow registry -> single-user bot ->
end-to-end test.

## Deliberately NOT done yet
Docker, Redis, Celery, PostgreSQL, cloud anything, MCP server, Claude routing, inline
buttons, video jobs, retention/cleanup. All are later phases or explicitly out of scope.
