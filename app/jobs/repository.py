"""Persistence and the state machine for jobs.

Two rules are enforced here rather than trusted to callers:

1. **Ownership.** Every lookup that serves a user takes their Telegram id and filters
   on it. There is no way to reach another user's job or output through this API.
2. **Legal transitions only.** `transition()` refuses any move not in
   `ALLOWED_TRANSITIONS`, so a race or a bug cannot leave the queue in a state the
   worker does not understand.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.database.connection import Database, from_iso, to_iso
from app.jobs.models import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    Job,
    JobOutput,
    JobStatus,
    utcnow,
)
from app.utils.logging import get_logger

log = get_logger(__name__)

_JOB_COLUMNS = """
    id, telegram_user_id, telegram_chat_id, telegram_message_id, original_request,
    workflow_id, parameters, status, comfy_prompt_id, error_message, user_message,
    retry_count, cancel_requested, created_at, updated_at, started_at, finished_at
"""

#: Jobs in these states are waiting for, or occupying, the GPU worker.
ACTIVE_STATUSES = (
    JobStatus.RECEIVED,
    JobStatus.QUEUED,
    JobStatus.PREPARING,
    JobStatus.GENERATING,
)


class JobRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # ---------------- creation ----------------

    def create(self, job: Job) -> Job:
        now = utcnow()
        job.created_at = now
        job.updated_at = now
        with self._db.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    telegram_user_id, telegram_chat_id, telegram_message_id,
                    original_request, workflow_id, parameters, status,
                    retry_count, cancel_requested, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.telegram_user_id,
                    job.telegram_chat_id,
                    job.telegram_message_id,
                    job.original_request,
                    job.workflow_id,
                    job.parameters_json(),
                    job.status.value,
                    job.retry_count,
                    int(job.cancel_requested),
                    to_iso(now),
                    to_iso(now),
                ),
            )
            job.id = int(cursor.lastrowid)

        log.info(
            "job.created",
            job_id=job.id,
            user_id=job.telegram_user_id,
            workflow=job.workflow_id,
        )
        return job

    # ---------------- lookup ----------------

    def get(self, job_id: int) -> Job | None:
        """Unscoped lookup. For the worker and admin paths only - never to serve a
        user a job by id. Use `get_for_user` for anything user-facing."""
        row = self._db.query_one(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,))
        return self._hydrate(row)

    def get_for_user(self, job_id: int, telegram_user_id: int) -> Job | None:
        """Ownership-scoped lookup. Another user's job id simply does not exist here."""
        row = self._db.query_one(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ? AND telegram_user_id = ?",
            (job_id, telegram_user_id),
        )
        return self._hydrate(row)

    def get_by_prompt_id(self, comfy_prompt_id: str) -> Job | None:
        row = self._db.query_one(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE comfy_prompt_id = ?", (comfy_prompt_id,)
        )
        return self._hydrate(row)

    def history_for_user(
        self, telegram_user_id: int, *, limit: int = 10, status: JobStatus | None = None
    ) -> list[Job]:
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE telegram_user_id = ?"
        params: list[Any] = [telegram_user_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))
        return [self._hydrate(row) for row in self._db.query_all(sql, tuple(params))]

    def latest_completed_for_user(self, telegram_user_id: int) -> Job | None:
        """Backs "upscale that" / "animate the last one".

        Scoped to the asking user by construction, so an implicit reference can never
        resolve to somebody else's image.
        """
        row = self._db.query_one(
            f"""SELECT {_JOB_COLUMNS} FROM jobs
                WHERE telegram_user_id = ? AND status = ?
                ORDER BY id DESC LIMIT 1""",
            (telegram_user_id, JobStatus.COMPLETED.value),
        )
        return self._hydrate(row)

    # ---------------- queue ----------------

    def next_queued(self) -> Job | None:
        """The oldest job waiting for the GPU. One worker, strict FIFO."""
        row = self._db.query_one(
            f"""SELECT {_JOB_COLUMNS} FROM jobs
                WHERE status = ? AND cancel_requested = 0
                ORDER BY id ASC LIMIT 1""",
            (JobStatus.QUEUED.value,),
        )
        return self._hydrate(row)

    def queue_snapshot(self) -> list[Job]:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        rows = self._db.query_all(
            f"""SELECT {_JOB_COLUMNS} FROM jobs
                WHERE status IN ({placeholders}) ORDER BY id ASC""",
            tuple(s.value for s in ACTIVE_STATUSES),
        )
        return [self._hydrate(row) for row in rows]

    def queue_position(self, job_id: int) -> int | None:
        """1-based position among waiting jobs; None once it is no longer waiting."""
        row = self._db.query_one(
            """SELECT COUNT(*) AS ahead FROM jobs
               WHERE status = ? AND id < ?
                 AND cancel_requested = 0""",
            (JobStatus.QUEUED.value, job_id),
        )
        job = self.get(job_id)
        if job is None or job.status is not JobStatus.QUEUED:
            return None
        return int(row["ahead"]) + 1

    def count_active(self) -> int:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        row = self._db.query_one(
            f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({placeholders})",
            tuple(s.value for s in ACTIVE_STATUSES),
        )
        return int(row["n"])

    def count_for_user_since(self, telegram_user_id: int, since: datetime) -> int:
        """Backs daily quotas. Cancelled jobs do not count against a user."""
        row = self._db.query_one(
            """SELECT COUNT(*) AS n FROM jobs
               WHERE telegram_user_id = ? AND created_at >= ? AND status != ?""",
            (telegram_user_id, to_iso(since), JobStatus.CANCELLED.value),
        )
        return int(row["n"])

    def count_for_user_today(self, telegram_user_id: int) -> int:
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return self.count_for_user_since(telegram_user_id, midnight)

    # ---------------- state changes ----------------

    def transition(
        self,
        job_id: int,
        to_status: JobStatus,
        *,
        comfy_prompt_id: str | None = None,
        error_message: str | None = None,
        user_message: str | None = None,
        expect: JobStatus | None = None,
    ) -> Job:
        """Move a job to `to_status`, refusing any illegal move.

        `expect` makes the update conditional on the current status, which is how two
        actors - the worker and a `/cancel` - are kept from clobbering each other.
        """
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT status, started_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"job {job_id} does not exist")

            current = JobStatus(row["status"])
            if expect is not None and current is not expect:
                raise InvalidTransition(job_id, current, to_status)
            if to_status not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(job_id, current, to_status)

            now = utcnow()
            fields = ["status = ?", "updated_at = ?"]
            params: list[Any] = [to_status.value, to_iso(now)]

            if to_status is JobStatus.PREPARING and row["started_at"] is None:
                fields.append("started_at = ?")
                params.append(to_iso(now))
            if to_status.is_terminal:
                fields.append("finished_at = ?")
                params.append(to_iso(now))
            if comfy_prompt_id is not None:
                fields.append("comfy_prompt_id = ?")
                params.append(comfy_prompt_id)
            if error_message is not None:
                fields.append("error_message = ?")
                params.append(error_message[:2000])
            if user_message is not None:
                fields.append("user_message = ?")
                params.append(user_message[:500])

            params.append(job_id)
            connection.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", tuple(params))

        log.info(
            "job.transition",
            job_id=job_id,
            **{"from": current.value, "to": to_status.value},
        )
        job = self.get(job_id)
        assert job is not None  # noqa: S101 - just updated it inside a transaction
        return job

    def request_cancel(self, job_id: int, telegram_user_id: int | None = None) -> bool:
        """Flag a job for cancellation. Returns False if it is already finished.

        Passing `telegram_user_id` scopes the request to that owner; omit it only for
        admin paths.
        """
        sql = "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status IN (?, ?, ?, ?)"
        params: list[Any] = [
            to_iso(utcnow()),
            job_id,
            *(s.value for s in ACTIVE_STATUSES),
        ]
        if telegram_user_id is not None:
            sql += " AND telegram_user_id = ?"
            params.append(telegram_user_id)

        with self._db.transaction() as connection:
            cursor = connection.execute(sql, tuple(params))
            changed = cursor.rowcount > 0

        if changed:
            log.info("job.cancel_requested", job_id=job_id, user_id=telegram_user_id)
        return changed

    def is_cancel_requested(self, job_id: int) -> bool:
        row = self._db.query_one("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,))
        return bool(row and row["cancel_requested"])

    def set_parameters(self, job_id: int, parameters: dict[str, Any]) -> None:
        """Record the sanitised parameters actually sent to ComfyUI."""
        import json

        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET parameters = ?, updated_at = ? WHERE id = ?",
                (json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                 to_iso(utcnow()), job_id),
            )

    def increment_retry(self, job_id: int) -> int:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
                (to_iso(utcnow()), job_id),
            )
            row = connection.execute(
                "SELECT retry_count FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return int(row["retry_count"]) if row else 0

    # ---------------- outputs ----------------

    def add_output(self, output: JobOutput) -> JobOutput:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO job_outputs (job_id, path, kind, size_bytes, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    output.job_id,
                    str(output.path),
                    output.kind,
                    output.size_bytes,
                    to_iso(output.created_at),
                ),
            )
            output.id = int(cursor.lastrowid)
        return output

    def outputs_for_job(self, job_id: int) -> list[JobOutput]:
        rows = self._db.query_all(
            """SELECT id, job_id, path, kind, size_bytes, created_at
               FROM job_outputs WHERE job_id = ? ORDER BY id ASC""",
            (job_id,),
        )
        return [self._hydrate_output(row) for row in rows]

    def outputs_for_user_job(self, job_id: int, telegram_user_id: int) -> list[JobOutput]:
        """Ownership-scoped. Returns nothing for a job the user does not own."""
        rows = self._db.query_all(
            """SELECT o.id, o.job_id, o.path, o.kind, o.size_bytes, o.created_at
               FROM job_outputs o
               JOIN jobs j ON j.id = o.job_id
               WHERE o.job_id = ? AND j.telegram_user_id = ?
               ORDER BY o.id ASC""",
            (job_id, telegram_user_id),
        )
        return [self._hydrate_output(row) for row in rows]

    # ---------------- recovery ----------------

    def interrupted_jobs(self) -> list[Job]:
        """Jobs that were mid-flight when the application stopped."""
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        rows = self._db.query_all(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status IN ({placeholders}) ORDER BY id ASC",
            tuple(s.value for s in ACTIVE_STATUSES),
        )
        return [self._hydrate(row) for row in rows]

    def stale_jobs(self, older_than: timedelta) -> list[Job]:
        cutoff = utcnow() - older_than
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        rows = self._db.query_all(
            f"""SELECT {_JOB_COLUMNS} FROM jobs
                WHERE status IN ({placeholders}) AND updated_at < ?""",
            (*(s.value for s in ACTIVE_STATUSES), to_iso(cutoff)),
        )
        return [self._hydrate(row) for row in rows]

    # ---------------- hydration ----------------

    def _hydrate(self, row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        job = Job(
            id=row["id"],
            telegram_user_id=row["telegram_user_id"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_message_id=row["telegram_message_id"],
            original_request=row["original_request"],
            workflow_id=row["workflow_id"],
            parameters=Job.parse_parameters(row["parameters"]),
            status=JobStatus(row["status"]),
            comfy_prompt_id=row["comfy_prompt_id"],
            error_message=row["error_message"],
            user_message=row["user_message"],
            retry_count=row["retry_count"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=from_iso(row["created_at"]) or utcnow(),
            updated_at=from_iso(row["updated_at"]) or utcnow(),
            started_at=from_iso(row["started_at"]),
            finished_at=from_iso(row["finished_at"]),
        )
        job.outputs = self.outputs_for_job(job.id) if job.id else []
        return job

    @staticmethod
    def _hydrate_output(row: sqlite3.Row) -> JobOutput:
        return JobOutput(
            id=row["id"],
            job_id=row["job_id"],
            path=Path(row["path"]),
            kind=row["kind"],
            size_bytes=row["size_bytes"],
            created_at=from_iso(row["created_at"]) or utcnow(),
        )
