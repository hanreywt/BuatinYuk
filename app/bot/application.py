"""Assembles the whole application and runs it.

This is the only module that knows about every layer at once. Everything else depends
on its neighbours through narrow interfaces, which is what keeps the worker testable
without Telegram and the orchestrator testable without a GPU.

Startup order matters:

1. open the database and load workflows - fail fast if either is broken
2. check ComfyUI, but do not require it: the bot should come up and *say* it is offline
3. reconcile jobs interrupted by the last shutdown, before the worker can touch them
4. start the worker, then start long polling
"""

from __future__ import annotations

import asyncio
from typing import Any

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot import handlers
from app.bot.notifier import TelegramNotifier
from app.comfy.client import ComfyUIClient
from app.config.settings import Settings
from app.database.connection import Database
from app.jobs.repository import JobRepository
from app.orchestrator.recovery import RecoveryService
from app.orchestrator.service import Orchestrator
from app.orchestrator.worker import GenerationWorker
from app.services.uploads import UploadService
from app.users.service import UserService
from app.utils.logging import get_logger
from app.workflows.registry import WorkflowRegistry

log = get_logger(__name__)


class GenerationServer:
    """Owns every component's lifetime."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._database: Database | None = None
        self._comfy: ComfyUIClient | None = None
        self._worker: GenerationWorker | None = None
        self._application: Application | None = None

    def build(self) -> Application:
        settings = self._settings
        settings.ensure_directories()

        self._database = Database(settings.database_path).connect()
        jobs = JobRepository(self._database)

        registry = WorkflowRegistry.load(settings.workflow_dir)
        if len(registry) == 0:
            raise RuntimeError(
                f"no usable workflows in {settings.workflow_dir}. "
                "Every template failed to load; check the log for the reason."
            )
        if settings.default_workflow not in registry:
            raise RuntimeError(
                f"DEFAULT_WORKFLOW is {settings.default_workflow!r}, which is not "
                f"installed. Available: {', '.join(registry.ids())}"
            )

        self._comfy = ComfyUIClient(settings.comfyui_base_url, settings.comfyui_ws_url)

        users = UserService(
            admin_ids=settings.admin_telegram_ids,
            jobs=jobs,
            default_daily_quota=settings.default_daily_quota,
        )

        application = (
            ApplicationBuilder()
            .token(settings.telegram_bot_token.get_secret_value())
            .post_init(self._on_start)
            .post_shutdown(self._on_shutdown)
            .build()
        )

        self._worker = GenerationWorker(
            repository=jobs,
            registry=registry,
            comfy=self._comfy,
            output_dir=settings.output_dir,
            notifier=TelegramNotifier(application.bot),
            job_timeout=float(settings.job_timeout_seconds),
        )

        application.bot_data["orchestrator"] = Orchestrator(
            users=users,
            jobs=jobs,
            registry=registry,
            worker=self._worker,
            comfy=self._comfy,
            default_workflow=settings.default_workflow,
            uploads=UploadService(comfy=self._comfy, input_dir=settings.input_dir),
            image_workflow=settings.image_workflow,
            video_workflow=settings.video_workflow,
            image_video_workflow=settings.image_video_workflow,
        )
        application.bot_data["jobs"] = jobs
        application.bot_data["settings"] = settings

        _register_handlers(application)
        application.add_error_handler(_on_error)

        self._application = application
        return application

    # ---------------- lifecycle hooks ----------------

    async def _on_start(self, application: Application) -> None:
        settings = self._settings
        assert self._comfy is not None and self._worker is not None  # noqa: S101

        me = await application.bot.get_me()
        log.info("bot.connected", username=me.username, bot_id=me.id)

        status = await self._comfy.status()
        if status.online:
            log.info(
                "comfy.ready",
                version=status.version,
                vram_free_gb=status.vram_free_gb,
                devices=status.devices,
            )
        else:
            # Not fatal: the bot should run and report the outage rather than refuse
            # to start, so an admin can ask it what is wrong.
            log.warning("comfy.offline_at_startup", url=settings.comfyui_base_url)

        jobs: JobRepository = application.bot_data["jobs"]
        report = await RecoveryService(
            repository=jobs, comfy=self._comfy, output_dir=settings.output_dir
        ).reconcile()
        if report.total:
            log.info("startup.recovery", summary=report.summary())

        await self._worker.start()
        log.info("server.ready", admins=len(settings.admin_telegram_ids))

    async def _on_shutdown(self, application: Application) -> None:
        if self._worker is not None:
            await self._worker.stop()
        if self._comfy is not None:
            await self._comfy.aclose()
        if self._database is not None:
            self._database.close()
        log.info("server.stopped")

    def run(self) -> None:
        """Blocking. Long polling only - no webhook, no inbound network surface."""
        application = self._application or self.build()
        application.run_polling(
            drop_pending_updates=True,  # do not act on messages sent while we were down
            allowed_updates=["message"],
        )


def _register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("generate", handlers.generate))
    application.add_handler(CommandHandler("video", handlers.video))
    application.add_handler(CommandHandler("status", handlers.status))
    application.add_handler(CommandHandler("queue", handlers.queue))
    application.add_handler(CommandHandler("history", handlers.history))
    application.add_handler(CommandHandler("cancel", handlers.cancel))
    application.add_handler(CommandHandler("workflows", handlers.workflows))

    # A photo, or an image sent as a file, with its caption as the prompt.
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handlers.photo)
    )

    # Anything else that is plain text becomes a generation request.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.freeform)
    )

    # Anything left over gets an explanation rather than silence.
    application.add_handler(MessageHandler(~filters.COMMAND, handlers.unsupported))


async def _on_error(update: object, context: Any) -> None:
    """Last resort. Handlers already catch their own errors; this catches the rest."""
    log.exception("bot.unhandled_error", error=str(context.error))
