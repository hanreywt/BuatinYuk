# BuatinDong

A private, local-first AI generation server driven from Telegram.

```
Telegram user
  -> Telegram bot (long polling)
    -> local Python orchestrator   (auth, quotas, job records, queue)
      -> [Claude interpretation layer, only when natural language needs it]
        -> local MCP server        (narrow, validated tools)
          -> local ComfyUI HTTP/WS API
            -> local GPU
              -> output file on this machine
                -> sent back to the originating chat
```

Everything except the Telegram Bot API (and, later, Anthropic inference) runs on one
machine. ComfyUI stays bound to `127.0.0.1`. Nothing is exposed publicly.

> **Status: Phase 0 complete — environment audited, skeleton in place, no application code
> yet.** See [PROJECT_STATUS.md](PROJECT_STATUS.md) for exactly what works, what was
> discovered about this machine, and what to do next.

---

## Prerequisites

| Requirement | This machine |
|---|---|
| Python 3.11+ | 3.11.9 ✅ |
| Git | 2.49.0 ✅ |
| NVIDIA GPU | RTX 3080, 10 GB ✅ |
| ComfyUI running locally | Desktop 0.33.2 on `127.0.0.1:8188` ✅ |
| Telegram bot token | ❌ not yet created |

## Setup

```powershell
# 1. Virtual environment (from the project root)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Dependencies
pip install -r requirements.txt

# 3. Configuration
Copy-Item .env.example .env
#    then edit .env and fill in the real values

# 4. Verify the machine
python scripts/audit_env.py
```

`audit_env.py` is read-only. It reports OS, Python, GPU, ComfyUI reachability, whether
ComfyUI is publicly bound, installed models, available workflows, and secret hygiene.
Run it after any ComfyUI upgrade or model change. Exit code 0 means ComfyUI answered.

## Configuration

All settings live in `.env` (gitignored). `.env.example` documents every key with fake
placeholders. Nothing secret belongs in source, in this README, or in a commit.

| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). Treat as a password. |
| `ADMIN_TELEGRAM_IDS` | Comma-separated **numeric** IDs. From [@userinfobot](https://t.me/userinfobot). |
| `COMFYUI_HOST` / `COMFYUI_PORT` | Keep at `127.0.0.1` / `8188`. |
| `COMFYUI_OUTPUT_DIR` | Where ComfyUI writes. This app reads it; it never writes there. |
| `DATABASE_PATH` | SQLite file under `data/`. |
| `OUTPUT_DIR` | Where this app stores per-job copies of results. |
| `DEFAULT_WORKFLOW` | Workflow ID used when the user does not pick one. |
| `DEFAULT_DAILY_QUOTA` | Jobs per day for the `USER` role. |

Identity is **always** the numeric Telegram user ID. Usernames are display metadata only —
they are changeable and must never be used for authorisation.

## Telegram bot setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts.
2. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
3. Message [@userinfobot](https://t.me/userinfobot) to get your numeric ID; put it in
   `ADMIN_TELEGRAM_IDS`.
4. Recommended with BotFather: `/setprivacy` → **Enabled**, and **do not** add the bot to
   group chats you do not control.

The bot uses **long polling** — no webhook, no public URL, no tunnel, no port forwarding.

If a token ever leaks (pasted into a chat, committed, logged), revoke it immediately with
BotFather `/revoke` and issue a new one.

## ComfyUI assumptions

This project treats ComfyUI as an **external local service** and never modifies its
installation. It only calls the HTTP/WS API and reads finished files.

- Must be running and reachable at `COMFYUI_HOST:COMFYUI_PORT` before a job can run.
- Must stay on loopback. Do not start it with `--listen`.
- Its output directory is read-only from this app's perspective.

## Workflows

Workflows are **approved templates stored in `workflows/`**, never graphs invented at
request time. Each has two files:

| File | Purpose |
|---|---|
| `<id>.api.json` | The ComfyUI **API-format** graph. Treated as opaque; never hand-edited. |
| `<id>.meta.json` | Which node inputs are editable, their types, bounds, and defaults. |

Only inputs listed in the `.meta.json` are ever substituted, and every value is validated
against its declared type and bounds first. A user prompt can change a mapped string field;
it can never reach a filename, a path, or an unmapped node.

### Adding a workflow

The most reliable source is a run that already succeeded on this machine:

```powershell
# List recent successful runs and their prompt_ids
python scripts/audit_env.py --json
```

Then either capture the API graph from ComfyUI's execution history (what
`txt2img_h3_plate` was built from — it is a byte-accurate record of something that
actually ran), or in the ComfyUI UI use **Workflow → Export (API)**. Save the graph as
`workflows/<id>.api.json`, write a matching `<id>.meta.json`, and re-run the audit.

The UI-format files under ComfyUI's `user/default/workflows/` are **not** API format and
cannot be POSTed to `/prompt` directly.

## Running

Not yet implemented — Phase 1. This section will cover starting the bot and the MCP server.

## Troubleshooting

| Symptom | Check |
|---|---|
| Audit says ComfyUI unreachable | Is the ComfyUI Desktop app actually running? Is it on port 8188? |
| Audit says "Bound publicly: YES" | ComfyUI was started with `--listen`. Remove it. |
| `checkpoints: (none)` | Expected on this machine — see PROJECT_STATUS.md. |
| Generation is very slow | 46 GB of weights on a 10 GB card means heavy offloading. Normal here. |
| Bot does not respond | Token wrong, or your numeric ID is not in `ADMIN_TELEGRAM_IDS`. |

Detailed diagnostics go to `logs/`. Messages shown to Telegram users are deliberately
generic — no paths, no stack traces, no machine details.

## Adding users safely

Planned for Phase 3, designed now so it is not bolted on later:

- The bot is **never** open to the public. Unknown IDs are rejected and logged.
- Access is granted by explicit admin approval, or by a **one-time invite code** that is
  random, expiring, single-use, and can never grant `ADMIN`.
- Roles: `ADMIN` (manage users, quotas, queue) · `TRUSTED` (higher quota) · `USER` (daily quota).
- Every output is owned by the user who requested it. One user's history, files, and
  "upscale that last one" references can never resolve to another user's results.

## Layout

```
app/
  bot/           Telegram transport only
  orchestrator/  request -> job, the decision layer
  comfy/         ComfyUI HTTP/WS client
  jobs/          job model, state machine, queue
  users/         identity, roles, quotas, invites
  database/      SQLite access
  services/      cross-cutting services
  config/        typed settings loaded from .env
  utils/         logging, path safety
mcp_server/      narrow local MCP tools
workflows/       approved graph templates + parameter metadata
outputs/         generated media (gitignored)
data/            SQLite database (gitignored)
logs/            structured logs (gitignored)
scripts/         operational scripts (audit_env.py)
tests/           unit tests
docs/            design notes
```

Each layer stays behind a clear interface. There is deliberately no single large `bot.py`.

## Security boundary

Telegram input is untrusted. A message can only ever become **validated parameters to an
approved workflow**. It can never become a shell command, a Python expression, a filesystem
path, a URL to fetch, an MCP configuration change, or a read of another user's files.

The MCP server exposes a narrow set of generation tools. It will not offer shell execution
or general filesystem access. Authorisation, quotas, and ownership are enforced by the
orchestrator in code — never by model reasoning alone.
