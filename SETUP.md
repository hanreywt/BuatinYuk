# Setup

Two ways to use this. Pick the one that matches what you want.

| | **Use someone's bot** | **Run your own** |
|---|---|---|
| What you need | Telegram | A gaming PC with an NVIDIA GPU |
| Setup time | 1 minute | 1–2 hours, mostly downloading |
| Disk space | none | ~50 GB |
| Cost | none | none (it all runs on your PC) |
| Read | [Part 1](#part-1--use-someone-elses-bot) | [Part 2](#part-2--run-your-own) |

---

# Part 1 — Use someone else's bot

Nothing to install. The generation happens on **their** computer.

1. Get your numeric Telegram ID: message [@userinfobot](https://t.me/userinfobot) — it
   replies with a number like `123456789`.
2. Send that number to whoever runs the bot. They add you.
3. Open the bot in Telegram and send `/start`.

Then just talk to it:

| Send this | Get |
|---|---|
| `a cinematic photo of Jakarta at night` | an image |
| `/video a neon street, slow camera push` | a short video with sound |
| a photo, with a caption describing what you want | an image based on your photo |
| a photo, caption starting with `/video` | a video animating your photo |

Useful commands: `/help` · `/status` · `/queue` · `/history` · `/cancel 12`

**Expect about two minutes.** There is one graphics card and jobs run one at a time, so
if someone else is generating, yours waits. `/queue` shows where you are.

---

# Part 2 — Run your own

## What you need first

| | |
|---|---|
| **GPU** | NVIDIA with **10 GB VRAM or more**. No GPU, no generation — this cannot run on a CPU in any useful way. |
| **Disk** | ~50 GB free. Almost all of it is the AI model. |
| **OS** | Windows, macOS, or Linux |
| **Python** | 3.11 or newer — [python.org/downloads](https://www.python.org/downloads/) (tick **"Add Python to PATH"** during install) |
| **Git** | [git-scm.com](https://git-scm.com/downloads) |

## Step 1 — Install ComfyUI

This project doesn't generate anything itself; it drives **ComfyUI**, which does.

Download **ComfyUI Desktop**: [comfy.org/download](https://www.comfy.org/download)

Install it, open it once to confirm it starts, and leave it running. It should be at
`http://127.0.0.1:8188`. Keep it on that address — do not enable any "listen" or
"share" option.

## Step 2 — Get the model files

This is the long part: roughly **46 GB**. They are far too large for the repository, so
you download them separately.

In ComfyUI, use **Manager → Model Manager** to search for and install the **MiniMax H3**
set, or place the files manually into your ComfyUI models folder:

| Folder | File | Size |
|---|---|---|
| `diffusion_models/` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 20 GB |
| `text_encoders/` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | 26 GB |
| `vae/` | `minimax_h3_video_vae_fp16.safetensors` | 5 GB |
| `vae/` | `minimax_h3_audio_vae_fp32.safetensors` | 0.6 GB |
| `loras/` | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 1.9 GB |

> **Using different models instead?** The workflow templates in `workflows/` name these
> files exactly, so they will not load anything else. See
> [Using your own models](#using-your-own-models) below.

## Step 3 — Make your Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → send `/newbot` → follow the prompts.
2. It gives you a **token** like `123456789:AAE...`. **Treat it like a password.**
3. Message [@userinfobot](https://t.me/userinfobot) → note your **numeric ID**.

## Step 4 — Get the project running

```bash
git clone <the repository URL>
cd BuatinDong

python -m venv .venv
```

Activate the environment:

```powershell
.\.venv\Scripts\Activate.ps1     # Windows PowerShell
```
```bash
source .venv/bin/activate         # macOS / Linux
```

Then:

```bash
pip install -r requirements.txt
```

## Step 5 — Configure it

Copy the example file and edit **the copy**:

```powershell
Copy-Item .env.example .env      # Windows
```
```bash
cp .env.example .env              # macOS / Linux
```

Open `.env` and set two lines:

```
TELEGRAM_BOT_TOKEN=<the token BotFather gave you>
ADMIN_TELEGRAM_IDS=<your numeric ID from @userinfobot>
```

> Edit **`.env`**, not `.env.example`. `.env` is ignored by git so your token never gets
> committed. Putting real values in `.env.example` publishes them — that mistake has
> already been made once on this project.

Set `COMFYUI_OUTPUT_DIR` if ComfyUI writes somewhere unusual. Everything else has a
sensible default.

## Step 6 — Check it before you start

```bash
python scripts/audit_env.py
```

This reads your machine and reports what it finds. You want:

- `Python ... (OK)` and a GPU listed
- `Bound publicly : no (localhost only)`
- your models under **Models**

If it says ComfyUI is unreachable, ComfyUI isn't running. Start it and try again.

Then prove generation works, without involving Telegram:

```bash
python scripts/verify_generation.py
```

It ends with `PASS` and writes files to `outputs/verify/`. **First run takes a few
minutes** — the model is loading. If this fails, the problem is ComfyUI or the models,
not the bot.

## Step 7 — Run it

```bash
python run_bot.py
```

Watch for `server.ready`. Then message your bot on Telegram and send `/start`.

Stop it with **Ctrl+C**. A job that was mid-generation is picked up and delivered when
you start it again.

**Dashboard**: while it runs, open **http://127.0.0.1:8765** to see the queue, what is
running, how long things take, and to pause the worker or cancel a job.

---

## What to expect

| | |
|---|---|
| Image | ~70–95 seconds |
| Video (with sound) | ~2 minutes |
| First job after starting | slower — the model is loading |
| Two jobs at once | never; they queue. One GPU, one job at a time, on purpose. |

Slower than that usually means something else is using your GPU.

---

## Letting friends in

The bot **ignores everyone** it does not know. That is deliberate: your GPU, your
electricity, your queue.

Open the dashboard at **http://127.0.0.1:8765** and click the **Admin** tab. Two ways to
let someone in:

**Add them directly.** Ask them to message
[@userinfobot](https://t.me/userinfobot) for their numeric ID, type it into the form, pick
a role and a daily limit, press Add. They can use the bot immediately.

**Send them a code.** Press *Create code*, send them the code, and they redeem it in
Telegram with `/redeem ABCD123456`. A code works **once**, expires, and can never make
someone an admin.

### Roles

| Role | Can do |
|---|---|
| `user` | Generate, up to their daily limit |
| `trusted` | The same, usually with a larger limit |
| `admin` | Everything, no limit, sees the whole queue |

You can disable someone temporarily, change their limit, or remove them entirely from the
same tab. The owners listed in `ADMIN_TELEGRAM_IDS` show as **from .env** and can only be
changed there — that way a mistake in the dashboard can never lock you out of it.

---

## Working on the code with Claude

This project was built with **Claude Code** — an AI assistant that reads the project,
writes code, runs the tests, and explains what it changed. You do not need it to *run*
the bot, only to change it.

### Install

You need [Node.js](https://nodejs.org) 18+ first, then:

```bash
npm install -g @anthropic-ai/claude-code
```

Check it worked:

```bash
claude --version
```

### Use it

```bash
cd BuatinDong
claude
```

It reads the project on its own. Ask in plain language:

```
read PROJECT_STATUS.md and tell me what this project does
why did my last job fail?
add a /ping command that replies pong
run the tests
```

Type `/exit` to leave. It asks before changing files or running commands — read what it
proposes rather than approving blindly.

### Start here

**`PROJECT_STATUS.md` is the handover document.** It records what works, measured
timings, the bugs already found and why they must not come back, and what to do next. A
good first message is literally:

```
read PROJECT_STATUS.md and SETUP.md, then tell me what you would work on next
```

### Worth knowing

- It needs an [Anthropic account](https://claude.ai). A subscription covers normal use;
  there is also pay-as-you-go.
- **It costs money to use Claude, but not to run the bot.** Generation happens on your own
  GPU and is free.
- Never paste your `.env`, your bot token, or any API key into a chat. Claude can read the
  files it needs by itself.
- Run `python -m pytest` after any change. There are 335 tests and they catch a lot.

Prefer a different editor? The project is plain Python — VS Code, PyCharm, or anything
else works. `.vscode/` already has run configurations if you use VS Code.

---

## Using your own models

The four templates in `workflows/` name specific model files, so they only work with the
MiniMax H3 set above. To use different models:

1. Build a workflow in ComfyUI's interface and **run it successfully at least once**.
2. Capture that exact graph — it is recorded in ComfyUI's history, so what you save is
   known to work rather than something hand-written.
3. Save it as `workflows/<name>.api.json`, and write `<name>.meta.json` beside it saying
   which node inputs may be changed. Copy an existing pair as a model.
4. Point `DEFAULT_WORKFLOW` in `.env` at it.

`README.md` has the detail.

---

## When something goes wrong

| What you see | What it usually means |
|---|---|
| `Configuration is incomplete` | `.env` is missing or a value is wrong. The message names the field. |
| Bot never answers | Wrong token, or your ID isn't in `ADMIN_TELEGRAM_IDS`. It must be the **number**, not your `@username`. |
| "You are not authorised" | Same — your numeric ID isn't listed. |
| "The image generator is offline" | ComfyUI isn't running. |
| `checkpoints: (none)` in the audit | Normal. This project uses `diffusion_models`, not checkpoints. |
| Everything is very slow | Normal on 10 GB VRAM — the model is bigger than the card and gets swapped in and out. |
| Out-of-memory errors | Close other GPU applications. Games and browsers with hardware acceleration are the usual culprits. |

Detailed logs are in `logs/app.log`. What the bot tells a user is deliberately vague —
it never reveals file paths or internal errors.

---

## Things worth knowing

- **Nothing is exposed to the internet.** The bot polls Telegram outbound; ComfyUI and
  the dashboard are localhost-only. There is no port to forward and no server to secure.
- **Everything stays on your machine** — images, videos, the database, the logs. The only
  things that leave are your prompt going to Telegram and the finished file coming back.
- **Never commit `.env`** or paste your token anywhere. If it leaks, revoke it
  immediately with BotFather `/revoke`.
- **Never put your token in `.env.example`** — that file *is* committed.
