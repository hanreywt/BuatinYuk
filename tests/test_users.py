"""Database-backed users and invitations."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.database.connection import Database, to_iso
from app.jobs.models import utcnow
from app.jobs.repository import JobRepository
from app.users.models import Role
from app.users.repository import InviteError, UserRepository, generate_code
from app.users.service import AccountDisabled, NotAuthorized, UserService

OWNER = 111       # from ADMIN_TELEGRAM_IDS
FRIEND = 222
STRANGER = 999


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "u.db").connect()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def users(db) -> UserRepository:
    return UserRepository(db)


@pytest.fixture
def service(db, users) -> UserService:
    return UserService(admin_ids=[OWNER], jobs=JobRepository(db), users=users)


# ---------------- who gets in ----------------


def test_a_configured_owner_is_always_an_admin(service: UserService) -> None:
    assert service.authorise(OWNER).is_admin
    assert service.is_bootstrap_admin(OWNER)


def test_a_configured_owner_cannot_be_locked_out_by_the_database(
    service: UserService, users: UserRepository
) -> None:
    """The database is what the admin tools edit, so it must not be able to lock the
    owner out of the tools that edit it."""
    users.upsert(OWNER, role=Role.USER, enabled=False)
    assert service.authorise(OWNER).is_admin


def test_a_stranger_is_refused(service: UserService) -> None:
    with pytest.raises(NotAuthorized):
        service.authorise(STRANGER)


def test_an_added_user_is_let_in(service: UserService, users: UserRepository) -> None:
    users.upsert(FRIEND, role=Role.USER, daily_quota=5, display_name="Sam")
    account = service.authorise(FRIEND)
    assert account.role is Role.USER
    assert account.daily_quota == 5
    assert not account.is_admin


def test_a_disabled_user_is_refused_but_still_listed(
    service: UserService, users: UserRepository
) -> None:
    users.upsert(FRIEND)
    users.set_enabled(FRIEND, False)
    with pytest.raises(AccountDisabled):
        service.authorise(FRIEND)
    assert FRIEND in [u.telegram_user_id for u in service.list_users()]


def test_a_removed_user_is_refused(service: UserService, users: UserRepository) -> None:
    users.upsert(FRIEND)
    assert users.remove(FRIEND) is True
    with pytest.raises(NotAuthorized):
        service.authorise(FRIEND)


def test_listing_shows_owners_then_the_database(
    service: UserService, users: UserRepository
) -> None:
    users.upsert(FRIEND)
    listed = service.list_users()
    assert [u.telegram_user_id for u in listed] == [OWNER, FRIEND]
    assert listed[0].is_admin and not listed[1].is_admin


def test_an_owner_is_not_listed_twice(service: UserService, users: UserRepository) -> None:
    users.upsert(OWNER, role=Role.USER)
    assert [u.telegram_user_id for u in service.list_users()].count(OWNER) == 1


# ---------------- editing ----------------


def test_quota_and_role_can_be_changed(service: UserService, users: UserRepository) -> None:
    users.upsert(FRIEND, daily_quota=3)
    assert users.set_quota(FRIEND, 25) is True
    assert users.set_role(FRIEND, Role.TRUSTED) is True

    account = service.authorise(FRIEND)
    assert account.daily_quota == 25 and account.role is Role.TRUSTED


def test_editing_an_unknown_user_reports_failure(users: UserRepository) -> None:
    assert users.set_quota(STRANGER, 5) is False
    assert users.set_enabled(STRANGER, True) is False
    assert users.remove(STRANGER) is False


def test_upsert_updates_rather_than_duplicating(users: UserRepository) -> None:
    users.upsert(FRIEND, daily_quota=5, display_name="Sam")
    users.upsert(FRIEND, daily_quota=20)
    assert len(users.list_all()) == 1
    assert users.get(FRIEND).daily_quota == 20
    # A name already recorded is not wiped by a later edit that omits it.
    assert users.get(FRIEND).display_name == "Sam"


# ---------------- invites ----------------


def test_generated_codes_are_random_and_unambiguous() -> None:
    codes = {generate_code() for _ in range(200)}
    assert len(codes) == 200  # no collisions in a sample this size
    assert all(len(c) == 10 for c in codes)
    # These characters are excluded because people type these codes by hand.
    assert not any(set(c) & set("O0I1l") for c in codes)


def test_an_invite_can_never_grant_admin(users: UserRepository) -> None:
    with pytest.raises(InviteError, match="admin"):
        users.create_invite(created_by=OWNER, role=Role.ADMIN)


def test_redeeming_creates_a_user(service: UserService, users: UserRepository) -> None:
    invite = users.create_invite(created_by=OWNER, role=Role.TRUSTED, daily_quota=30)
    account = service.redeem_invite(invite.code, FRIEND, "Sam")

    assert account.role is Role.TRUSTED
    assert account.daily_quota == 30
    assert service.authorise(FRIEND).telegram_user_id == FRIEND


def test_a_code_works_only_once(service: UserService, users: UserRepository) -> None:
    invite = users.create_invite(created_by=OWNER)
    service.redeem_invite(invite.code, FRIEND)

    with pytest.raises(InviteError, match="already used"):
        service.redeem_invite(invite.code, STRANGER)
    with pytest.raises(NotAuthorized):
        service.authorise(STRANGER)


def test_an_expired_code_is_refused(service: UserService, users: UserRepository, db) -> None:
    invite = users.create_invite(created_by=OWNER)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE invites SET expires_at = ? WHERE code = ?",
            (to_iso(utcnow() - timedelta(days=1)), invite.code),
        )
    with pytest.raises(InviteError, match="expired"):
        service.redeem_invite(invite.code, FRIEND)


def test_an_unknown_code_is_refused(service: UserService) -> None:
    with pytest.raises(InviteError, match="unknown"):
        service.redeem_invite("NOTACODE12", FRIEND)


def test_codes_are_matched_case_insensitively(service: UserService, users: UserRepository) -> None:
    """People retype these by hand; case is not a security property."""
    invite = users.create_invite(created_by=OWNER)
    assert service.redeem_invite(invite.code.lower(), FRIEND).telegram_user_id == FRIEND


def test_a_revoked_code_cannot_be_redeemed(service: UserService, users: UserRepository) -> None:
    invite = users.create_invite(created_by=OWNER)
    assert users.revoke_invite(invite.code) is True
    with pytest.raises(InviteError):
        service.redeem_invite(invite.code, FRIEND)


def test_a_used_code_cannot_be_revoked(users: UserRepository, service: UserService) -> None:
    """Revoking a used code would erase the record of how someone got in."""
    invite = users.create_invite(created_by=OWNER)
    service.redeem_invite(invite.code, FRIEND)
    assert users.revoke_invite(invite.code) is False


def test_invite_state_is_reported(users: UserRepository, service: UserService) -> None:
    unused = users.create_invite(created_by=OWNER)
    used = users.create_invite(created_by=OWNER)
    service.redeem_invite(used.code, FRIEND)

    states = {i.code: i.state() for i in users.list_invites()}
    assert states[unused.code] == "valid"
    assert states[used.code] == "used"


def test_a_used_invite_records_who_used_it(users: UserRepository, service: UserService) -> None:
    invite = users.create_invite(created_by=OWNER)
    service.redeem_invite(invite.code, FRIEND)
    assert users.get_invite(invite.code).used_by == FRIEND


def test_invites_are_unavailable_without_a_user_store(db) -> None:
    from app.users.service import AuthorizationError

    service = UserService(admin_ids=[OWNER], jobs=JobRepository(db), users=None)
    with pytest.raises(AuthorizationError) as exc:
        service.redeem_invite("ANYCODE123", FRIEND)

    assert "no user store" in str(exc.value)               # for the operator
    assert "not available" in exc.value.user_message       # for the person asking


# ---------------- quota still applies ----------------


def test_an_invited_user_gets_the_quota_from_their_code(
    service: UserService, users: UserRepository
) -> None:
    invite = users.create_invite(created_by=OWNER, daily_quota=2)
    account = service.redeem_invite(invite.code, FRIEND)
    assert service.check_quota(account).remaining == 2
