"""Telegram command handlers.

This layer is transport only. It reads what Telegram sent, calls the orchestrator, and
formats the reply. It performs no authorisation of its own - every entry point goes
through the orchestrator, which is where access is actually decided.

All of these commands are deterministic and never involve Claude. Natural-language
interpretation arrives in Phase 5, and will produce a `GenerationRequest` that still
passes through exactly the same orchestrator gates.
"""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.comfy.errors import ComfyError
from app.jobs.models import JobStatus
from app.orchestrator.service import GenerationRequest, Orchestrator
from app.users.service import AuthorizationError
from app.utils.logging import get_logger
from app.workflows.registry import WorkflowError

log = get_logger(__name__)

HELP_TEXT = """\
*Generation*
`/generate <description>` - generate an image
Or just send a message describing what you want.

*Your jobs*
`/status [job id]` - system status, or one job
`/queue` - what is waiting or running
`/history` - your recent jobs
`/cancel <job id>` - cancel a job of yours

*Reference*
`/workflows` - available workflows
`/help` - this message

Generation takes roughly two minutes. You will get the image when it is done.
"""

GENERIC_ERROR = "Something went wrong handling that. Please try again."


def _orchestrator(context: ContextTypes.DEFAULT_TYPE) -> Orchestrator:
    return context.application.bot_data["orchestrator"]


async def _reply(update: Update, text: str, *, markdown: bool = False) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN if markdown else None
    )


def _handle_errors(func):
    """Turn any expected failure into a safe sentence; log the rest.

    A user never sees a stack trace, a path, or an internal identifier.
    """

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await func(update, context)
        except (AuthorizationError, WorkflowError, ComfyError) as exc:
            log.info(
                "bot.refused",
                handler=func.__name__,
                user_id=update.effective_user.id if update.effective_user else None,
                reason=str(exc),
            )
            await _reply(update, exc.user_message)
        except Exception as exc:  # noqa: BLE001 - one bad update must not kill the bot
            log.exception(
                "bot.handler_error",
                handler=func.__name__,
                user_id=update.effective_user.id if update.effective_user else None,
                error=str(exc),
            )
            await _reply(update, GENERIC_ERROR)

    wrapper.__name__ = func.__name__
    return wrapper


# ---------------- basics ----------------


@_handle_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    orchestrator = _orchestrator(context)
    # authorise() raises for strangers, which the wrapper turns into the refusal text.
    account = orchestrator.users.authorise(user.id)

    quota = orchestrator.quota_for(account)
    await _reply(
        update,
        f"Ready. You are signed in as {account.role.value}.\n"
        f"Usage: {quota.describe()}\n\n"
        "Send a description of what you want generated, or /help for commands.",
    )


@_handle_errors
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _orchestrator(context).users.authorise(update.effective_user.id)
    await _reply(update, HELP_TEXT, markdown=True)


# ---------------- generation ----------------


@_handle_errors
async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = " ".join(context.args) if context.args else ""
    if not prompt.strip():
        await _reply(update, "Describe what you want generated, for example:\n"
                             "/generate a cinematic photo of Jakarta at night")
        return
    await _submit(update, context, prompt)


@_handle_errors
async def freeform(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain message is treated as a generation request.

    Deterministic on purpose: in v0.1 the text becomes the prompt directly, with no
    model in the loop, which keeps the common path fast, free, and predictable.
    """
    message = update.effective_message
    if message is None or not message.text:
        return
    await _submit(update, context, message.text)


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    message = update.effective_message
    accepted = await _orchestrator(context).submit(
        GenerationRequest(
            telegram_user_id=update.effective_user.id,
            telegram_chat_id=message.chat_id,
            telegram_message_id=message.message_id,
            text=prompt,
        )
    )
    await _reply(update, accepted.describe())


# ---------------- queries ----------------


@_handle_errors
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orchestrator = _orchestrator(context)
    user_id = update.effective_user.id

    if context.args:
        job_id = _parse_job_id(context.args[0])
        if job_id is None:
            await _reply(update, "Give a job number, for example /status 12")
            return
        job = await orchestrator.job_for_user(job_id, user_id)
        if job is None:
            # Same answer whether it never existed or belongs to someone else.
            await _reply(update, f"No job #{job_id} of yours.")
            return
        await _reply(update, await _describe_job(orchestrator, job))
        return

    system = await orchestrator.system_status(user_id)
    await _reply(update, system.describe())


@_handle_errors
async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orchestrator = _orchestrator(context)
    jobs = await orchestrator.queue_view(update.effective_user.id)
    if not jobs:
        await _reply(update, "The queue is empty.")
        return

    lines = ["Queue:"]
    for job in jobs:
        position = await orchestrator.queue_position(job.id)
        marker = f"#{position}" if position else job.status.value
        lines.append(f"  Job #{job.id} - {marker}")
    await _reply(update, "\n".join(lines))


@_handle_errors
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs = await _orchestrator(context).history(update.effective_user.id, limit=10)
    if not jobs:
        await _reply(update, "You have no jobs yet.")
        return

    lines = ["Your recent jobs:"]
    for job in jobs:
        request = job.original_request
        preview = request[:50] + ("…" if len(request) > 50 else "")
        lines.append(f"  #{job.id} [{job.status.value}] {preview}")
    await _reply(update, "\n".join(lines))


@_handle_errors
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update, "Give a job number, for example /cancel 12")
        return

    job_id = _parse_job_id(context.args[0])
    if job_id is None:
        await _reply(update, "Give a job number, for example /cancel 12")
        return

    cancelled = await _orchestrator(context).cancel(job_id, update.effective_user.id)
    if cancelled:
        await _reply(update, f"Job #{job_id} will be cancelled.")
    else:
        await _reply(update, f"Job #{job_id} is not one of yours, or has already finished.")


@_handle_errors
async def workflows(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orchestrator = _orchestrator(context)
    orchestrator.users.authorise(update.effective_user.id)

    available = orchestrator.registry.list()
    if not available:
        await _reply(update, "No workflows are installed.")
        return

    lines = []
    for workflow in available:
        lines.append(f"*{workflow.workflow_id}* - {workflow.display_name}")
        if workflow.description:
            lines.append(f"  {workflow.description}")
        lines.append(f"  settings: {', '.join(workflow.user_parameters)}")
    await _reply(update, "\n".join(lines), markdown=True)


# ---------------- helpers ----------------


def _parse_job_id(raw: str) -> int | None:
    candidate = raw.lstrip("#").strip()
    if not candidate.isdigit():
        return None
    value = int(candidate)
    return value if value > 0 else None


async def _describe_job(orchestrator: Orchestrator, job) -> str:
    lines = [f"Job #{job.id}: {job.status.value}"]

    if job.status is JobStatus.QUEUED:
        position = await orchestrator.queue_position(job.id)
        if position:
            lines.append(f"Queue position: {position}")
    if job.status is JobStatus.COMPLETED:
        lines.append(f"Outputs: {len(job.outputs)}")
        if job.duration_seconds:
            lines.append(f"Took {round(job.duration_seconds)}s")
    if job.status is JobStatus.FAILED and job.user_message:
        lines.append(job.user_message)

    request = job.original_request
    lines.append(f"Request: {request[:100]}{'…' if len(request) > 100 else ''}")
    return "\n".join(lines)
