"""Users, roles, and quotas.

Identity is **always** the numeric Telegram user id. Usernames are display metadata:
they are changeable by their owner and must never decide authorisation.

In v0.1 the only authorised users are the admin ids in configuration. The types here
are shaped for Phase 3, where users move into the database with invites and approval -
at which point only the lookup changes, not the callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.jobs.models import utcnow


class Role(str, Enum):
    ADMIN = "admin"
    TRUSTED = "trusted"
    USER = "user"

    @property
    def is_admin(self) -> bool:
        return self is Role.ADMIN


#: Daily job allowance per role. ADMIN is unmetered; the others are configurable.
UNLIMITED = -1


@dataclass(frozen=True, slots=True)
class User:
    telegram_user_id: int
    role: Role = Role.USER
    enabled: bool = True
    daily_quota: int = 10
    display_name: str | None = None  # metadata only, never used for authorisation
    created_at: datetime | None = None
    approved_by: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role.is_admin

    @property
    def has_unlimited_quota(self) -> bool:
        return self.role is Role.ADMIN or self.daily_quota == UNLIMITED

    def quota_remaining(self, used_today: int) -> int | None:
        """None means unlimited."""
        if self.has_unlimited_quota:
            return None
        return max(0, self.daily_quota - used_today)

    def label(self) -> str:
        """Safe identifier for logs and admin output - id first, name is decoration."""
        if self.display_name:
            return f"{self.telegram_user_id} ({self.display_name})"
        return str(self.telegram_user_id)


def admin(telegram_user_id: int) -> User:
    return User(
        telegram_user_id=telegram_user_id,
        role=Role.ADMIN,
        daily_quota=UNLIMITED,
        created_at=utcnow(),
    )
