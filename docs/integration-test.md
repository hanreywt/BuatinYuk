# Integration test: Telegram → ComfyUI → Telegram

The automated suite (`python -m pytest`) covers the logic without a GPU or a network.
This document covers the part that cannot be mocked: the real round trip.

Run it after any change to the bot, the worker, the ComfyUI client, or a workflow
template, and after any ComfyUI upgrade.

---

## A. Machine check (no GPU time)

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/audit_env.py
```

**Pass:** exit code 0, ComfyUI reachable, `Bound publicly: no (localhost only)`,
the expected models listed, `.env` present and gitignored.

---

## B. Generation check (no Telegram)

Exercises registry → validation → ComfyUI → local file, using the same code the bot uses.

```powershell
python scripts/verify_generation.py
```

**Pass:** ends with `PASS: registry -> ComfyUI -> local file works end to end.`
Files land in `outputs/verify/`.

Reference timing on the RTX 3080 with the MiniMax H3 stack, `length=5`, 608x1088,
12 steps: **≈122 s**, 5 PNGs of ≈875 KB. Substantially slower than that means the model
is being reloaded, or something else is using the GPU.

---

## C. Full round trip (Telegram → ComfyUI → Telegram)

Prerequisites: `.env` filled in with a real `TELEGRAM_BOT_TOKEN` and your numeric
`ADMIN_TELEGRAM_IDS`, and ComfyUI running.

```powershell
python run_bot.py
```

Expected on startup, in order: `bot.connected` with your bot's username, `comfy.ready`
with a version and VRAM, then `server.ready`. Leave it running and use Telegram.

| # | Send | Expect |
|---|------|--------|
| 1 | `/start` | Signed in as `admin`, with your usage line |
| 2 | `/help` | The command list |
| 3 | `/workflows` | `txt2img_h3_plate` and its settable parameters |
| 4 | `/status` | Generator online, worker idle, queue 0 |
| 5 | `a cinematic photo of Jakarta at night` | `Job #1 accepted. Starting now.` then `Job #1 is generating…`, and **an image about two minutes later** |
| 6 | `/history` | Job #1, `completed` |
| 7 | `/status 1` | `completed`, output count, duration |

### Queueing (two jobs)

Send two prompts quickly. The second must answer `Queue position: 2` and must not start
until the first finishes. `/queue` should list both. **A second generation starting
before the first finishes is a bug** — the design is strictly sequential.

### Cancellation

Send a prompt, then `/cancel <id>` while it runs. Expect `Job #N will be cancelled.`,
then the job reaching `cancelled`. `/cancel` on a finished job must report that it is
not cancellable, not raise.

### Rejection of unauthorised users

From a Telegram account **not** in `ADMIN_TELEGRAM_IDS`, send `/start` and a prompt.

**Pass:** both get the "not authorised" refusal, no job is created (`/queue` from your
admin account stays empty), and `logs/app.log` records `auth.denied` with the id.

---

## D. Failure handling

| Scenario | How to force it | Expect |
|---|---|---|
| ComfyUI offline | Quit ComfyUI, then send a prompt | The job fails with "The image generator is offline right now." No stack trace, no paths |
| ComfyUI offline at startup | Quit ComfyUI, then start the bot | Bot still starts; `/status` reports `Generator : OFFLINE` |
| Restart mid-generation | Send a prompt; Ctrl+C the bot while it generates; restart it | Startup logs `startup.recovery`. The finished result is recovered and delivered, **not** regenerated |
| Restart while queued | Queue two jobs; Ctrl+C during the first; restart | Queued work resumes; nothing is lost or duplicated |
| Overlong prompt | Send >2000 characters | Refused immediately with a length message; no job created |

---

## E. What to check in the logs afterwards

```powershell
Select-String -Path logs\app.log -Pattern "job.transition" | Select-Object -Last 20
```

- State transitions form a legal sequence (`received → queued → preparing → generating → completed`)
- **No secrets anywhere**: `Select-String -Path logs\app.log -Pattern "AA[A-Za-z0-9_-]{30,}"` returns nothing
- Errors carry an internal detail, while the message sent to Telegram does not

---

## Recording results

Note the date, what passed, and anything surprising in `PROJECT_STATUS.md` under
"Current working functionality", so the next session knows what was last verified on
real hardware rather than in tests.
