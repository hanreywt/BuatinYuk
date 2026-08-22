"""Delivers job results back to the originating Telegram conversation.

This is the only place the worker's progress reaches Telegram. Per the `JobNotifier`
contract nothing here raises: a Telegram outage must never stop the GPU queue, so
every failure is logged and swallowed.

Results always go to the chat recorded on the job, never to a chat supplied at
delivery time - which is what keeps one user's output out of another user's chat.
"""

from __future__ import annotations

from pathlib import Path

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import TelegramError

from app.comfy.models import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from app.jobs.models import Job
from app.utils.logging import get_logger

log = get_logger(__name__)

#: Telegram refuses photos above 10 MB; larger files go as documents instead.
PHOTO_SIZE_LIMIT = 10 * 1024 * 1024
#: Videos sent as playable media share the same 50 MB bot ceiling as documents.
VIDEO_SIZE_LIMIT = 50 * 1024 * 1024
#: And nothing above this can be sent by a bot at all.
DOCUMENT_SIZE_LIMIT = 50 * 1024 * 1024


class TelegramNotifier:
    """Implements `JobNotifier` against a python-telegram-bot `Bot`."""

    def __init__(self, bot: Bot, *, send_all_frames: bool = False) -> None:
        self._bot = bot
        # The H3 workflow returns several consecutive frames of one clip, which are
        # near-identical. Sending them all would spam the chat, so one is delivered
        # and the rest stay on disk.
        self._send_all_frames = send_all_frames

    async def job_started(self, job: Job) -> None:
        await self._say(job, f"Job #{job.id} is generating. This usually takes a couple of minutes.")
        await self._typing(job)

    async def job_progress(self, job: Job, message: str) -> None:
        await self._say(job, f"Job #{job.id}: {message}")

    async def job_completed(self, job: Job, files: list[Path]) -> None:
        if not files:
            await self._say(job, f"Job #{job.id} finished but produced no output.")
            return

        to_send = files if self._send_all_frames else files[:1]
        caption = f"Job #{job.id}"
        if len(files) > len(to_send):
            caption += f" ({len(files)} frames generated, showing the first)"

        delivered = 0
        for path in to_send:
            if await self._send_file(job, path, caption if delivered == 0 else None):
                delivered += 1

        if delivered == 0:
            await self._say(
                job, f"Job #{job.id} finished, but the result could not be uploaded."
            )

    async def job_failed(self, job: Job, user_message: str) -> None:
        await self._say(job, f"Job #{job.id}: {user_message}")

    # ---------------- delivery ----------------

    async def _send_file(self, job: Job, path: Path, caption: str | None) -> bool:
        try:
            size = path.stat().st_size
        except OSError as exc:
            log.warning("notify.output_missing", job_id=job.id, error=str(exc))
            return False

        if size > DOCUMENT_SIZE_LIMIT:
            log.warning("notify.output_too_large", job_id=job.id, bytes=size)
            await self._say(job, f"Job #{job.id} finished, but the file is too large to send.")
            return False

        suffix = path.suffix.lower()
        try:
            with path.open("rb") as handle:
                if suffix in VIDEO_SUFFIXES and size <= VIDEO_SIZE_LIMIT:
                    # Sent as a video so it plays inline with sound, rather than
                    # arriving as a file the user has to download first.
                    await self._bot.send_video(
                        chat_id=job.telegram_chat_id,
                        video=handle,
                        caption=caption,
                        supports_streaming=True,
                        reply_to_message_id=job.telegram_message_id,
                    )
                elif suffix in IMAGE_SUFFIXES and size <= PHOTO_SIZE_LIMIT:
                    await self._bot.send_photo(
                        chat_id=job.telegram_chat_id,
                        photo=handle,
                        caption=caption,
                        reply_to_message_id=job.telegram_message_id,
                    )
                else:
                    await self._bot.send_document(
                        chat_id=job.telegram_chat_id,
                        document=handle,
                        caption=caption,
                        reply_to_message_id=job.telegram_message_id,
                    )
        except TelegramError as exc:
            log.warning("notify.upload_failed", job_id=job.id, error=str(exc))
            # A reply target that no longer exists is the usual cause; retry unthreaded.
            return await self._retry_without_reply(job, path, caption)
        except OSError as exc:
            log.warning("notify.read_failed", job_id=job.id, error=str(exc))
            return False

        log.info("notify.delivered", job_id=job.id, bytes=size)
        return True

    async def _retry_without_reply(self, job: Job, path: Path, caption: str | None) -> bool:
        try:
            with path.open("rb") as handle:
                await self._bot.send_document(
                    chat_id=job.telegram_chat_id, document=handle, caption=caption
                )
        except (TelegramError, OSError) as exc:
            log.warning("notify.upload_failed_final", job_id=job.id, error=str(exc))
            return False
        return True

    async def _say(self, job: Job, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=job.telegram_chat_id, text=text)
        except TelegramError as exc:
            log.warning("notify.message_failed", job_id=job.id, error=str(exc))

    async def _typing(self, job: Job) -> None:
        try:
            await self._bot.send_chat_action(
                chat_id=job.telegram_chat_id, action=ChatAction.UPLOAD_PHOTO
            )
        except TelegramError:
            pass  # cosmetic only
