# CLAUDE.md

Guidance for Claude Code working in this repository.

**Read `PROJECT_STATUS.md` first.** It is the living handover: what works, measured
timings, bugs already found and why they must not return, and what to do next. Keep it
updated as you go — the next session depends on it.

## What this is

A private Telegram bot that generates images and video on a local GPU:

```
Telegram → orchestrator (auth, quota, jobs) → queue worker → local ComfyUI → GPU
        ← file delivered to the originating chat ←
```

Everything runs on one machine. The only outbound traffic is Telegram's API.

## Commands

```bash
python -m pytest              # 335 tests, no GPU or network needed
python scripts/audit_env.py   # read-only machine + ComfyUI report
python scripts/verify_generation.py   # real generation, uses the GPU (~2 min)
python scripts/check_secrets.py       # scan for credentials
python run_bot.py             # start the bot; dashboard on http://127.0.0.1:8765
```

Use `.venv/Scripts/python.exe` on Windows. Install the secret hook once per clone with
`python scripts/check_secrets.py --install` — git hooks are not cloned.

## Architecture, and why

Layers depend on their neighbours through narrow interfaces. There is deliberately no
single large `bot.py`.

| Layer | Holds |
|---|---|
| `app/bot/` | Telegram transport only. No authorisation decisions. |
| `app/orchestrator/` | The single gate: authorise → quota → workflow → validate → queue. Plus the worker and restart recovery. |
| `app/workflows/` | Approved graph templates and parameter validation. |
| `app/comfy/` | ComfyUI HTTP/WS client. |
| `app/jobs/` `app/users/` `app/database/` | State, identity, persistence. |
| `app/web/` | Local dashboard (loopback only). |

**The worker never imports Telegram.** It reports through `JobNotifier`, so the queue is
testable without a bot and a Telegram outage cannot stop the GPU.

## Rules that are load-bearing

These are security properties, not preferences. Changing them needs a reason.

- **Workflow graphs are captured from ComfyUI's execution history, never hand-written.**
  `/history` returns the exact API graph of every run and whether it succeeded, so a
  capture is known to work on this machine. See `docs/decisions.md` D1.
- **Only inputs declared in `<id>.meta.json` are ever substituted.** Model names, wiring,
  and filename fields are beyond reach of any request.
- **`managed` parameters (filename prefix, uploaded image) can only be set by the
  orchestrator**, never accepted from a request.
- **Identity is the numeric Telegram ID.** Usernames are display metadata; they change.
- **Configured admins (`ADMIN_TELEGRAM_IDS`) outrank the database**, because the database
  is what the admin tools edit.
- **Invites can never grant admin** — enforced at creation and again at redemption.
- **Uploads are validated by their leading bytes**, not by filename or reported MIME type.
- **Stored filenames come from job and owner IDs only.** User text never reaches a path.
- **Errors shown to users carry no paths, stack traces, or internal IDs.** Every error
  class has a safe `user_message`; detail goes to `logs/`.
- **One job at a time.** One 10 GB card running a model that already offloads would thrash
  on two. Sequential is deliberate.
- **A submitted job is never resubmitted after a restart.** Recovery asks ComfyUI what
  happened; the worker adopts in-flight jobs. Duplicating costs minutes of GPU time.
- **ComfyUI and the dashboard stay on loopback.** Long polling only — nothing inbound.

## Conventions

- Tests are the specification. Name them for the behaviour, not the method:
  `test_a_user_cannot_read_another_users_job`, not `test_get_for_user`.
- Comments explain *why*, and only where the reason is not obvious from the code.
- Add a regression test for every bug fixed — `PROJECT_STATUS.md` lists eight so far.
- New dependencies need justification. The stack is stdlib `sqlite3`, `httpx`,
  `python-telegram-bot`, `aiohttp`, `pydantic`. No Docker, Redis, Celery, or ORM.
- Run the full suite before committing. The pre-commit hook blocks credentials.

## Working with ComfyUI

It is an external service that may be offline or restarted at any moment. Never modify its
installation. Completion is decided by polling `/history`, which survives a dropped
socket; the WebSocket only feeds progress and its failure is non-fatal.

To add a workflow: build it in ComfyUI, **run it successfully once**, capture the graph
from `/history` into `workflows/<id>.api.json`, and write `<id>.meta.json` declaring the
editable inputs. UI-format exports are rejected at load time — they cannot be POSTed to
`/prompt`.

## This machine

RTX 3080, 10 GB VRAM. The only models installed are the **MiniMax H3** video stack (~46 GB)
— there are no image checkpoints, so a still image is a 5-frame clip. A cold run spends
~56s loading the text encoder before sampling starts. Expect ~70–95s for an image, ~120s
for video.

## Never

- Commit `.env`, tokens, or keys. Never put a real value in `.env.example` — that mistake
  has already cost one token here.
- Expose ComfyUI, the dashboard, or the database to the network.
- Let a Telegram message become a shell command, a filesystem path, or a URL to fetch.
- Trust model reasoning for authorisation. The orchestrator decides, in code.
