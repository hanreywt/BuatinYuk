# BuatinYuk

**Generate images and video from Telegram, on your own GPU.**

Send a message to your bot, get a picture back. Send `/video`, get a short clip with
sound. Send a photo with a caption, get that photo reimagined or animated. Everything
runs on one machine — your machine — through a local [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
install. No cloud generation, no public endpoint, no port forwarding.

```
Telegram  →  bot (long polling)  →  orchestrator  →  local ComfyUI  →  your GPU
                                    auth · quotas · queue                 ↓
              your chat  ←────────────────────── generated image / video
```

---

## What it does

| Send in Telegram | Get back |
|---|---|
| any text | a still image (~70–95s) |
| `/video <text>` | a short clip with sound (~120s) |
| a photo + caption | a still, generated from that photo |
| a photo + `/video` caption | a clip animating that photo |

Plus `/status`, `/queue`, `/history`, `/cancel <id>`, `/workflows` — all deterministic,
with no model in the loop.

**A local dashboard** at `http://127.0.0.1:8765` shows the live queue, what is running
and for how long, and per-workflow averages. You can pause the worker, cancel a job, add
people by Telegram ID, and issue single-use invite codes.

## Why it's built this way

- **Local-first.** ComfyUI stays bound to `127.0.0.1` and is never modified by this app.
  The only thing that leaves the machine is the Telegram API call.
- **Approved workflows only.** Generation runs from templates in `workflows/`, never from
  graphs invented at request time. A user's prompt can fill in a mapped, type-and-bounds
  checked field — it can never reach a file path, a shell, or an unmapped node.
- **Real multi-user.** Identity is the numeric Telegram ID, never the username. Roles,
  daily quotas, and per-user output ownership are enforced in code by the orchestrator.
- **Layered, not one big `bot.py`.** Transport, orchestration, ComfyUI client, jobs,
  users, and storage each sit behind their own interface.

## Stack

Python 3.11 · `python-telegram-bot` · `httpx` / `websockets` · Pydantic · SQLite ·
structlog · aiohttp (loopback dashboard) · ComfyUI

335 tests pass with `python -m pytest` — no GPU or network needed.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                # then fill in the real values
python scripts/audit_env.py                         # read-only machine + ComfyUI check
python run_bot.py
```

You'll need a bot token from [@BotFather](https://t.me/BotFather) and your numeric ID
from [@userinfobot](https://t.me/userinfobot).

Never seen this project before? Read **[SETUP.md](SETUP.md)** — it's written for exactly
that.

## Setting it up with Claude Code, Codex, or Cursor

If you'd rather not follow the steps by hand, open a terminal in an empty folder, start
your coding agent, and paste this:

```text
Set up the BuatinYuk Telegram image/video bot on this machine:
https://github.com/hanreywt/BuatinYuk

Work through it with me, one step at a time, and stop to ask whenever you
need something only I can provide.

1. Clone the repo and read SETUP.md and docs/reference.md before doing anything.
2. Check my machine first: NVIDIA GPU with 10 GB+ VRAM, ~50 GB free disk,
   Python 3.11+. Tell me plainly if something is missing instead of working
   around it — without a GPU this cannot generate anything.
3. Tell me whether ComfyUI is installed and reachable at http://127.0.0.1:8188,
   and whether the MiniMax H3 model files listed in SETUP.md are present.
   If they're missing, tell me exactly which ones and where they go — the
   downloads are mine to do, they're ~46 GB.
4. Create the virtualenv, install requirements.txt, and copy .env.example to .env.
5. Ask me for my Telegram bot token (from @BotFather) and my numeric Telegram ID
   (from @userinfobot), then write them into .env. Do not print the token back
   to me and never commit .env.
6. Run `python scripts/audit_env.py` and use its output to fill in
   COMFYUI_OUTPUT_DIR in .env.
7. Run `python -m pytest` — all tests should pass with no GPU and no network.
8. Install the secret-scanning hook: `python scripts/check_secrets.py --install`.
9. Start it with `python run_bot.py`, then tell me to send /start to my bot and
   open the dashboard at http://127.0.0.1:8765.

Rules while you work: don't modify my ComfyUI installation, keep ComfyUI and the
dashboard on 127.0.0.1, and don't put any real secret in a file other than .env.
```

The repo ships a [CLAUDE.md](CLAUDE.md) with the architecture and the rules that must
not be broken, so an agent working inside the project picks that up automatically.

**Two things it can't do for you:** downloading ~46 GB of model weights, and creating
your bot with [@BotFather](https://t.me/BotFather). Everything else it can drive.

## Project layout

```
app/bot/           Telegram transport only
app/orchestrator/  request → job, the decision layer
app/comfy/         ComfyUI HTTP/WS client
app/jobs/          job model, state machine, queue
app/users/         identity, roles, quotas, invites
app/web/           loopback dashboard
workflows/         approved graph templates + parameter metadata
scripts/           audit_env.py, verify_generation.py, check_secrets.py
```

## Docs

- **[SETUP.md](SETUP.md)** — full setup, for someone new to the project
- **[docs/reference.md](docs/reference.md)** — configuration, workflows, dashboard, troubleshooting
- **[PROJECT_STATUS.md](PROJECT_STATUS.md)** — what works today and what's next

## Licence

MIT — see [LICENSE](LICENSE). Model weights are not included and carry their own terms.
