# PROJECT STATUS

Living handover document. **Any Claude Code session picking this project up should read
this file first**, then run `python scripts/audit_env.py` to re-verify the machine.

Last updated: 2026-08-22 · Current version: **v0.4 — running in daily use**

---

## What works right now

The bot is **live and in real use**. From Telegram:

| Send | Get |
|---|---|
| text | still image (~70–95s) |
| `/video <text>` | short clip with sound (~120s) |
| photo + caption | still, generated from that image |
| photo + `/video` caption | clip animating that image |
| `/status` `/queue` `/history` `/cancel <id>` `/workflows` `/help` | deterministic, no model in the loop |

Plus a **local dashboard** at `http://127.0.0.1:8765` — live queue, what is running and
for how long, per-workflow averages, pause/resume the worker, cancel a job.

**311 tests pass** (`python -m pytest`). Everything below was verified on this machine,
not assumed.

---

## Phase checklist

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Environment audit, project skeleton, docs | **DONE** |
| 1 | Telegram → ComfyUI MVP | **DONE**, verified live |
| 2 | Persistent jobs + sequential GPU queue | **DONE** |
| 3 | Multi-user, roles, invites, quotas, admin commands | **PARTIAL** — role/quota engine and ownership scoping built and tested; users still come from `ADMIN_TELEGRAM_IDS`. DB-backed users, invite codes, admin commands remain |
| 4 | Local MCP server | NOT STARTED |
| 5 | Claude natural-language interpretation | NOT STARTED |
| 6 | Upscale, inline buttons, retention | NOT STARTED |
| — | Image input, video output, local dashboard | **DONE** (ahead of the original plan) |

---

## Measured performance (real runs on this machine)

| Workflow | Output | Time |
|---|---|---|
| `txt2img_h3_plate` | 5 PNGs (~875 KB each), one delivered | ~67–95s |
| `img2img_h3` | 5 PNGs from an input image | ~77–93s |
| `txt2video_h3` | 1 mp4 with audio, ~1.4 MB | ~116s |
| `img2video_h3` | 1 mp4 with audio, ~1.1 MB | ~133–198s |

The dashboard computes these live from completed jobs, so they stay current.

**Where the time goes**: on a cold run, ~56s of a 67s image job was loading the 25.9 GB
text encoder; sampling was only ~37s. Models stay resident between jobs (GPU sits at
~8.9/10.2 GB), so back-to-back jobs are much cheaper — but **switching between image and
video workflows costs a reload**, because the video graph adds the audio VAE.

---

## Discovered environment (verified 2026-08-22)

Re-run `python scripts/audit_env.py` to refresh.

### Host
- Windows 11 Pro, **Python 3.11.9**, Git 2.49, Claude Code 2.1.238
- **NVIDIA RTX 3080, 10 GB VRAM**, driver 591.86
- Disk: `D:` ~99 GB free · `C:` ~26 GB free — worth watching

### ComfyUI
- **Desktop build 0.33.2**, torch 2.12.1+cu130, its own Python 3.13
- `http://127.0.0.1:8188` — **loopback only, no `--listen`**. Keep it that way.
- Install: `D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI`
- Output: `D:\Comfy-Desktop\ComfyUI-Shared\output` · Input: `...\input` · Models: `...\models`
- 37 saved `Rey_*` UI workflows — a useful source of verified graphs

### Models — the whole stack is one video model
No image checkpoints exist. Everything runs on **MiniMax H3**:
`minimax_h3_fl2va_pruned_int8_convrot` (20 GB) + `qwen3vl_32b_minimax_h3_int8_convrot`
text encoder (25.9 GB) + video VAE (5 GB) + **audio VAE** (0.6 GB) + an 8-step turbo LoRA.
Also installed but unused: `film_net_fp16` (frame interpolation), `birefnet` (background
removal).

`checkpoints/`, `controlnet/`, `upscale_models/` are all empty.

**A still is a 5-frame clip.** `length=5` is the node minimum, so a single image costs 5
frames regardless; four are kept on disk and one delivered.

---

## Installed workflows

All four live in `workflows/` as `<id>.api.json` + `<id>.meta.json`.

| ID | Task | Source |
|---|---|---|
| `txt2img_h3_plate` | text → image | captured from history `f813cb25`, verified success |
| `img2img_h3` | image + text → image | the above plus `LoadImage → ImageScale → first_frame` |
| `txt2video_h3` | text → video+audio | captured from history `eeb409b9`, input-image branch removed |
| `img2video_h3` | image + text → video+audio | `eeb409b9` verbatim |

**Templates are captured from ComfyUI's own execution history, never authored by hand.**
`/history` returns the exact API graph of every run including whether it succeeded, so a
capture is known to execute here. See `docs/decisions.md` D1.

Only inputs declared in `.meta.json` are ever substituted. A parameter may declare
multiple `targets` — `width` drives both the generation *and* the input-image scaler,
which must not be allowed to drift apart.

---

## Bugs found and fixed (do not reintroduce)

1. **`PREPARING` could not return to `QUEUED`** — recovery silently failed to requeue an
   unsubmitted job. `GENERATING → QUEUED` is now explicitly *illegal*; that is the one
   that would duplicate GPU work.
2. **A single admin id crashed startup** — pydantic-settings JSON-decodes complex fields,
   so `ADMIN_TELEGRAM_IDS=12345` arrived as an `int` while `111,222` arrived as a `str`.
   The field is now a plain string parsed by the model.
3. **Declared defaults were decorative** — `build()` only substituted values passed in, so
   the captured template's `length=73` won every time. Every one-image request rendered 73
   frames and took 218s instead of ~122s.
4. **The seed never changed** — baked at `20250822`, so the same prompt returned a
   byte-identical image forever. Now rolled per job and recorded.
5. **Photos did nothing** — only `filters.TEXT` was registered, and a Telegram photo
   carries a *caption*, not text. Silent no-op with no log line.
6. **Cancelling a queued job did nothing** — it set the flag, the worker skipped the job,
   and nothing ever moved it. It sat in `queued` for good, still counted as active.
7. **A restart orphaned an in-flight job** — recovery rightly refused to resubmit, but
   nothing picked it back up. The worker now adopts `GENERATING` jobs and resumes waiting.
8. **Recovered results were never delivered** — recovery downloaded the file, marked the
   job complete, and stopped. From the user's side the request simply vanished.

A **secret leak** also happened: a real bot token was pasted into `.env.example` (a
tracked file) and swept into two commits by `git add -A`. The token has been revoked, and
`scripts/check_secrets.py` now runs as a pre-commit hook. The old value remains in commits
`d8287f0`/`551531c` by the owner's deliberate choice — it is revoked and the repo has no
remote, so it is not an outstanding risk.

---

## Known gaps / next steps

Ranked by what daily use has exposed, not by the original phase order.

1. **Effective parameters are not recorded.** Jobs show `params {}` because only values
   explicitly passed in get stored — defaults applied inside `build()` do not. The
   database therefore cannot say what size or length actually ran, and a result is not
   reliably reproducible. **Fix this before building on top of it.**
2. **Inline buttons** — Regenerate / Animate this / Variations under each result. Kills
   the retype-and-reupload loop. The ownership scoping it needs is already built and tested.
3. **Frame interpolation** — `film_net_fp16.safetensors` is installed and unused. Doubles
   video smoothness or length for a fraction of what sampling those frames costs.
4. **`steps=12` on images may waste a third of sampling.** The turbo LoRA is an 8-step
   LoRA and the video graphs use 8. Worth an A/B at the same seed.
5. **Only portrait.** Everything is centre-cropped to 768×1024 or 608×1088. Orientation
   presets would stop landscape sources losing their sides.
6. **Draft mode** — generate at ~448×768 while iterating, full size for keepers.
7. **Queue ordering by loaded workflow** — would avoid model reloads, at the cost of
   strict FIFO fairness. Owner's call.
8. Phase 3 proper (DB users, invites, admin commands), Phase 4 (MCP), Phase 5 (Claude).
9. An upscale model (~64 MB); `upscale_models/` is empty.
10. Retention — outputs only grow. Job 1 alone left 73 PNGs.

---

## Operating it

```powershell
cd d:\Rey_August\BuatinDong
.\.venv\Scripts\Activate.ps1
python run_bot.py            # Ctrl+C stops cleanly
python scripts/audit_env.py  # read-only machine check
python -m pytest             # 311 tests, no GPU or network needed
```

VS Code: **Run & Debug → "Run bot"**, or **Tasks → "Start bot"**. The dashboard is at
`http://127.0.0.1:8765` while it runs.

A clean Ctrl+C finishes the current step; anything mid-generation is adopted and delivered
on the next start.

---

## Security posture

- ComfyUI on loopback (verified via `argv`, no `--listen`); the dashboard binds to
  loopback and refuses any other host at startup; Telegram uses **long polling** — no
  webhook, no tunnel, no forwarded port, nothing inbound.
- Identity is the **numeric** Telegram id. Usernames are display metadata only.
- Uploads are validated by their leading bytes, not by filename or the MIME type Telegram
  reports; a renamed executable is refused before touching disk.
- Stored filenames are built from job and owner ids only — user text never reaches a path.
- Errors shown to users carry no paths, stack traces, or internal ids; detail stays in
  `logs/`. Secrets are redacted centrally before anything is written.
- `.env` is gitignored and a pre-commit secret scan is installed.
