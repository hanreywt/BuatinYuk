"""Authorisation and quota enforcement.

Every request passes through `authorise()` before anything else happens. Nothing
downstream re-checks identity, so this is the single place where access is decided -
which is exactly why it must never depend on model reasoning or on a username.

v0.1 backs the user list with the configured admin ids. Phase 3 replaces
`_lookup` with a database query; callers are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.jobs.repository import JobRepository
from app.users.models import Role, User, admin
from app.utils.logging import get_logger

log = get_logger(__name__)

DENIED_MESSAGE = (
    "You are not authorised to use this bot. If you think you should be, "
    "ask the owner to add you."
)


class AuthorizationError(Exception):
    """The request must not proceed. `user_message` is safe to show."""

    def __init__(self, message: str, user_message: str) -> None:
        super().__init__(message)
        self.user_message = user_message


class NotAuthorized(AuthorizationError):
    def __init__(self, telegram_user_id: int) -> None:
        super().__init__(f"user {telegram_user_id} is not authorised", DENIED_MESSAGE)


class AccountDisabled(AuthorizationError):
    def __init__(self, telegram_user_id: int) -> None:
        super().__init__(
            f"user {telegram_user_id} is disabled",
            "Your access to this bot has been disabled.",
        )


class QuotaExceeded(AuthorizationError):
    def __init__(self, telegram_user_id: int, quota: int) -> None:
        super().__init__(
            f"user {telegram_user_id} exceeded a daily quota of {quota}",
            f"You have reached your daily limit of {quota} generations. "
            "It resets at midnight UTC.",
        )


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    used_today: int
    limit: int | None  # None means unlimited
    remaining: int | None

    @property
    def unlimited(self) -> bool:
        return self.limit is None

    def describe(self) -> str:
        if self.unlimited:
            return f"{self.used_today} today (no limit)"
        return f"{self.used_today}/{self.limit} today ({self.remaining} left)"


class UserService:
    def __init__(
        self,
        *,
        admin_ids: Sequence[int],
        jobs: JobRepository,
        default_daily_quota: int = 10,
    ) -> None:
        self._admins = frozenset(admin_ids)
        self._jobs = jobs
        self._default_quota = default_daily_quota

        if not self._admins:
            log.warning("auth.no_admins_configured")

    # ---------------- lookup ----------------

    def _lookup(self, telegram_user_id: int) -> User | None:
        """The only source of identity. Phase 3 swaps this for a database query."""
        if telegram_user_id in self._admins:
            return admin(telegram_user_id)
        return None

    def get(self, telegram_user_id: int) -> User | None:
        return self._lookup(telegram_user_id)

    def is_known(self, telegram_user_id: int) -> bool:
        return self._lookup(telegram_user_id) is not None

    def is_admin(self, telegram_user_id: int) -> bool:
        user = self._lookup(telegram_user_id)
        return user is not None and user.is_admin

    def list_admins(self) -> list[int]:
        return sorted(self._admins)

    # ---------------- gates ----------------

    def authorise(self, telegram_user_id: int) -> User:
        """Identity and enablement. Raises rather than returning a falsy value, so a
        caller cannot forget to check the result."""
        user = self._lookup(telegram_user_id)
        if user is None:
            log.warning("auth.denied", user_id=telegram_user_id)
            raise NotAuthorized(telegram_user_id)
        if not user.enabled:
            log.warning("auth.disabled", user_id=telegram_user_id)
            raise AccountDisabled(telegram_user_id)
        return user

    def authorise_admin(self, telegram_user_id: int) -> User:
        user = self.authorise(telegram_user_id)
        if not user.is_admin:
            log.warning("auth.admin_denied", user_id=telegram_user_id)
            # Deliberately the same message a stranger gets: an ordinary user learns
            # nothing about which commands exist.
            raise NotAuthorized(telegram_user_id)
        return user

    def check_quota(self, user: User) -> QuotaStatus:
        """Read-only. Raises QuotaExceeded when the user has nothing left."""
        used = self._jobs.count_for_user_today(user.telegram_user_id)
        if user.has_unlimited_quota:
            return QuotaStatus(used_today=used, limit=None, remaining=None)

        limit = user.daily_quota if user.daily_quota >= 0 else self._default_quota
        remaining = max(0, limit - used)
        if remaining <= 0:
            raise QuotaExceeded(user.telegram_user_id, limit)
        return QuotaStatus(used_today=used, limit=limit, remaining=remaining)

    def quota_status(self, user: User) -> QuotaStatus:
        """Same as `check_quota` but never raises - for /status displays."""
        try:
            return self.check_quota(user)
        except QuotaExceeded as exc:
            limit = user.daily_quota if user.daily_quota >= 0 else self._default_quota
            del exc
            return QuotaStatus(
                used_today=self._jobs.count_for_user_today(user.telegram_user_id),
                limit=limit,
                remaining=0,
            )
