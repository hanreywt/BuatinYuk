# PROJECT STATUS

Living handover document. **Any Claude Code session picking this project up should read
this file first**, then run `python scripts/audit_env.py` to re-verify the machine.

Last updated: 2026-08-22 · Current version: **v0.1 code complete — awaiting live Telegram test**

---

## Phase checklist

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Environment audit, project skeleton, docs | **DONE** |
| 1 | Telegram -> ComfyUI MVP (single fixed workflow, single user) | **CODE COMPLETE**, needs live test |
| 2 | Persistent job system + sequential GPU queue (SQLite) | **DONE** (bot integration pending) |
| 3 | Multi-user, roles, invites, quotas, admin commands | PARTIAL — roles/quota engine built, DB-backed users + invites pending |
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

| SQLite layer | `app/database/connection.py` | Done. WAL, foreign keys on, `PRAGMA user_version` migrations, refuses a database written by a newer schema. |
| Job model + state machine | `app/jobs/models.py` | Done. 7 states with an explicit legal-transition table. |
| Job repository | `app/jobs/repository.py` | Done. Ownership-scoped queries, guarded transitions, FIFO queue, quota counting. |
| GPU queue worker | `app/orchestrator/worker.py` | Done. Strictly one job at a time; survives a failing notifier, a bad workflow, and ComfyUI going away. |
| Startup recovery | `app/orchestrator/recovery.py` | Done. Reconciles interrupted jobs without duplicating GPU work. |
| Notifier protocol | `app/orchestrator/notifier.py` | Done. Keeps Telegram out of the worker. |

| Users, roles, quotas | `app/users/` | Done. Identity is the numeric Telegram id only. v0.1 authorises from `ADMIN_TELEGRAM_IDS`; Phase 3 swaps `UserService._lookup` for a DB query and nothing else changes. |
| Orchestrator | `app/orchestrator/service.py` | Done. The single gate: authorise → quota → workflow → validate → create → queue. |
| Telegram notifier | `app/bot/notifier.py` | Done. Never raises; falls back from photo to document, and retries unthreaded if the reply target is gone. |
| Telegram handlers | `app/bot/handlers.py` | Done. `/start /help /generate /status /queue /history /cancel /workflows`, plus plain text as a prompt. |
| Application wiring | `app/bot/application.py`, `run_bot.py` | Done. Verified to build, gate, and query live ComfyUI with a fake token. |

**v0.1 is code complete.** Everything that can be verified without a Telegram token has
been. What remains is section C of `docs/integration-test.md` — the live round trip.

### Measured generation performance (real run, 2026-08-22)

`python scripts/verify_generation.py` — full chain registry → ComfyUI → local PNG, **PASS**.

| Measure | Value |
|---|---|
| Total wall time | **122 s** at `length=5`, 608x1088, 12 steps |
| Text encoder load | ~56 s (first ~56 s before sampling starts) |
| Sampling | ~37 s (12 steps, ~1.7 s/step) |
| VAE decode + save | ~26 s |
| Outputs | **5 PNGs**, ~875 KB each |
| Image quality | Good — strong prompt adherence |

**Implication for the bot**: `length=5` yields 5 *consecutive frames of one clip*, so they
are near-identical. Deliver **one** to Telegram and keep the rest on disk. Do not send five
near-duplicates.

**Implication for timeouts and UX**: ~2 minutes per request means the user must get an
immediate acknowledgement and a progress update, never a silent wait. `JOB_TIMEOUT_SECONDS`
of 1800 is comfortable.

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

## Open questions — BLOCKING the live test

1. **Telegram bot token** — owner is creating it. Needed in `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Owner's numeric Telegram user ID** — needed as `ADMIN_TELEGRAM_IDS`. From `@userinfobot`.
3. ~~Image strategy~~ — **DECIDED 2026-08-22**: use the H3 stack as-is. An SDXL/Flux
   workflow can be added later as another template; the registry supports it with no
   code change.
4. ~~Unverified `length=5` path~~ — **VERIFIED 2026-08-22**, see the measured numbers below.

---

## Security posture (current)
- ComfyUI on loopback only — verified via `argv`, no `--listen` flag.
- No secrets exist yet. `.env.example` holds placeholders only; `.env` is gitignored.
- No public tunnel, webhook, or port forward. Telegram will use **long polling**.
- Nothing has been committed to git (repo not initialised).

---

## Bugs found and fixed during development
Both were found by tests or a wiring check, not in production — worth recording so they
are not reintroduced.

1. **`PREPARING` could not return to `QUEUED`.** Startup recovery silently failed to
   requeue a job that had never been submitted, because the transition was missing from
   the state machine and the `except` branch swallowed it. `PREPARING → QUEUED` is now
   legal, `GENERATING → QUEUED` is now explicitly illegal (that is the one that would
   duplicate GPU work), and the branch logs instead of returning quietly.
2. **A single admin id crashed startup.** `pydantic-settings` JSON-parses env values for
   complex types, so `ADMIN_TELEGRAM_IDS=12345` arrived as an `int` while `111,222`
   arrived as a `str`. The validator only handled strings — meaning the single-admin
   case, the most likely real configuration, failed. It now accepts int, str, list, and
   tuple, and rejects non-numeric input with a message naming @userinfobot.

## Next concrete action
1. Owner fills `.env` with the bot token and their numeric Telegram id.
2. Run `python run_bot.py` and work through **section C** of
   [docs/integration-test.md](docs/integration-test.md) — the live round trip, queueing,
   cancellation, and the unauthorised-user rejection.
3. Record the result in this file.
4. Then Phase 3 proper (DB-backed users, invites, admin commands) or Phase 4 (MCP),
   depending on whether friends need access before Claude interpretation does.

## Deliberately NOT done yet
Docker, Redis, Celery, PostgreSQL, cloud anything, MCP server, Claude routing, inline
buttons, video jobs, retention/cleanup. All are later phases or explicitly out of scope.
