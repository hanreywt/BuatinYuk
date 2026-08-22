"""Persistence for people and invitations.

Two rules live here because they are security properties, not preferences:

1. **An invite can never grant ADMIN.** Admin rights come from `ADMIN_TELEGRAM_IDS` or
   from an existing admin promoting someone deliberately - never from redeeming a code,
   however that code was obtained.
2. **A code is single-use and expiring.** Redemption is one atomic statement that both
   checks and consumes, so the same code cannot be redeemed twice by two people racing.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta

from app.database.connection import Database, from_iso, to_iso
from app.jobs.models import utcnow
from app.users.models import Role, User
from app.utils.logging import get_logger

log = get_logger(__name__)

#: Unambiguous alphabet - no O/0, I/1/l - because these get typed by hand.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 10
DEFAULT_INVITE_DAYS = 7


class InviteError(Exception):
    """The code cannot be redeemed. `user_message` is safe to show."""

    def __init__(self, message: str, user_message: str) -> None:
        super().__init__(message)
        self.user_message = user_message


class Invite:
    __slots__ = ("code", "role", "daily_quota", "note", "created_by", "created_at",
                 "expires_at", "used_at", "used_by")

    def __init__(self, **kw) -> None:
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < utcnow()

    @property
    def is_valid(self) -> bool:
        return not self.is_used and not self.is_expired

    def state(self) -> str:
        if self.is_used:
            return "used"
        return "expired" if self.is_expired else "valid"


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


class UserRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # ---------------- users ----------------

    def get(self, telegram_user_id: int) -> User | None:
        row = self._db.query_one(
            "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        return self._hydrate(row)

    def list_all(self) -> list[User]:
        rows = self._db.query_all("SELECT * FROM users ORDER BY created_at ASC")
        return [self._hydrate(row) for row in rows]

    def upsert(
        self,
        telegram_user_id: int,
        *,
        role: Role = Role.USER,
        daily_quota: int = 10,
        display_name: str | None = None,
        note: str | None = None,
        approved_by: int | None = None,
        enabled: bool = True,
    ) -> User:
        now = utcnow()
        with self._db.transaction() as connection:
            connection.execute(
                """INSERT INTO users (telegram_user_id, role, enabled, daily_quota,
                                      display_name, note, approved_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (telegram_user_id) DO UPDATE SET
                       role = excluded.role,
                       enabled = excluded.enabled,
                       daily_quota = excluded.daily_quota,
                       display_name = COALESCE(excluded.display_name, users.display_name),
                       note = COALESCE(excluded.note, users.note),
                       updated_at = excluded.updated_at""",
                (telegram_user_id, role.value, int(enabled), daily_quota,
                 display_name, note, approved_by, to_iso(now), to_iso(now)),
            )
        log.info("user.saved", user_id=telegram_user_id, role=role.value, enabled=enabled)
        return self.get(telegram_user_id)  # type: ignore[return-value]

    def set_enabled(self, telegram_user_id: int, enabled: bool) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE users SET enabled = ?, updated_at = ? WHERE telegram_user_id = ?",
                (int(enabled), to_iso(utcnow()), telegram_user_id),
            )
        changed = cursor.rowcount > 0
        if changed:
            log.info("user.enabled" if enabled else "user.disabled", user_id=telegram_user_id)
        return changed

    def set_quota(self, telegram_user_id: int, daily_quota: int) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE users SET daily_quota = ?, updated_at = ? WHERE telegram_user_id = ?",
                (daily_quota, to_iso(utcnow()), telegram_user_id),
            )
        return cursor.rowcount > 0

    def set_role(self, telegram_user_id: int, role: Role) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE users SET role = ?, updated_at = ? WHERE telegram_user_id = ?",
                (role.value, to_iso(utcnow()), telegram_user_id),
            )
        if cursor.rowcount > 0:
            log.info("user.role_changed", user_id=telegram_user_id, role=role.value)
            return True
        return False

    def remove(self, telegram_user_id: int) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            )
        if cursor.rowcount > 0:
            log.info("user.removed", user_id=telegram_user_id)
            return True
        return False

    # ---------------- invites ----------------

    def create_invite(
        self,
        *,
        created_by: int,
        role: Role = Role.USER,
        daily_quota: int = 10,
        note: str | None = None,
        valid_days: int = DEFAULT_INVITE_DAYS,
    ) -> Invite:
        if role is Role.ADMIN:
            # Admin rights are granted deliberately by an admin, never by holding a code.
            raise InviteError(
                "invites cannot grant admin",
                "Invites cannot grant admin rights.",
            )

        now = utcnow()
        expires = now + timedelta(days=max(1, min(valid_days, 90)))
        code = generate_code()
        with self._db.transaction() as connection:
            connection.execute(
                """INSERT INTO invites (code, role, daily_quota, note, created_by,
                                        created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (code, role.value, daily_quota, note, created_by,
                 to_iso(now), to_iso(expires)),
            )
        log.info("invite.created", created_by=created_by, role=role.value, expires=to_iso(expires))
        return self.get_invite(code)  # type: ignore[return-value]

    def get_invite(self, code: str) -> Invite | None:
        row = self._db.query_one("SELECT * FROM invites WHERE code = ?", (code.strip().upper(),))
        return self._hydrate_invite(row)

    def list_invites(self, *, include_used: bool = True) -> list[Invite]:
        sql = "SELECT * FROM invites"
        if not include_used:
            sql += " WHERE used_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT 100"
        return [self._hydrate_invite(row) for row in self._db.query_all(sql)]

    def revoke_invite(self, code: str) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM invites WHERE code = ? AND used_at IS NULL",
                (code.strip().upper(),),
            )
        return cursor.rowcount > 0

    def redeem(self, code: str, telegram_user_id: int, display_name: str | None = None) -> User:
        """Consume a code and create the user. Raises InviteError if it cannot be used.

        The check and the consume are one statement inside one transaction, so two
        people racing on the same code cannot both win.
        """
        normalised = code.strip().upper()
        now = utcnow()

        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM invites WHERE code = ?", (normalised,)
            ).fetchone()
            if row is None:
                raise InviteError(f"unknown invite {normalised!r}", "That invite code is not valid.")
            if row["used_at"] is not None:
                raise InviteError(
                    f"invite {normalised!r} already used",
                    "That invite code has already been used.",
                )
            expires = from_iso(row["expires_at"])
            if expires is not None and expires < now:
                raise InviteError(
                    f"invite {normalised!r} expired", "That invite code has expired."
                )

            cursor = connection.execute(
                "UPDATE invites SET used_at = ?, used_by = ? WHERE code = ? AND used_at IS NULL",
                (to_iso(now), telegram_user_id, normalised),
            )
            if cursor.rowcount == 0:
                raise InviteError(
                    f"invite {normalised!r} was consumed concurrently",
                    "That invite code has already been used.",
                )

            role = Role(row["role"])
            if role is Role.ADMIN:  # belt and braces; creation already refuses this
                role = Role.USER

            connection.execute(
                """INSERT INTO users (telegram_user_id, role, enabled, daily_quota,
                                      display_name, note, approved_by, created_at, updated_at)
                   VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (telegram_user_id) DO UPDATE SET
                       enabled = 1, updated_at = excluded.updated_at""",
                (telegram_user_id, role.value, row["daily_quota"], display_name,
                 f"invited with {normalised}", row["created_by"], to_iso(now), to_iso(now)),
            )

        log.info("invite.redeemed", user_id=telegram_user_id, role=role.value)
        return self.get(telegram_user_id)  # type: ignore[return-value]

    # ---------------- hydration ----------------

    @staticmethod
    def _hydrate(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(
            telegram_user_id=row["telegram_user_id"],
            role=Role(row["role"]),
            enabled=bool(row["enabled"]),
            daily_quota=row["daily_quota"],
            display_name=row["display_name"],
            created_at=from_iso(row["created_at"]),
            approved_by=row["approved_by"],
        )

    @staticmethod
    def _hydrate_invite(row: sqlite3.Row | None) -> Invite | None:
        if row is None:
            return None
        return Invite(
            code=row["code"],
            role=Role(row["role"]),
            daily_quota=row["daily_quota"],
            note=row["note"],
            created_by=row["created_by"],
            created_at=from_iso(row["created_at"]),
            expires_at=from_iso(row["expires_at"]),
            used_at=from_iso(row["used_at"]),
            used_by=row["used_by"],
        )
