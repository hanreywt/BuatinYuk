"""Configuration parsing and secret hygiene."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import PROJECT_ROOT, Settings

BASE = {"telegram_bot_token": "123456789:AAFakeTokenForTests", "comfyui_output_dir": "."}


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


# ---------------- admin ids ----------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (12345, (12345,)),          # a single id arrives from the env as an int
        ("12345", (12345,)),
        ("111,222", (111, 222)),
        ("111, 222", (111, 222)),
        ("111 222", (111, 222)),
        ([111, 222], (111, 222)),   # a JSON list in the env arrives as a list
        ((111, 222), (111, 222)),
        ("", ()),
    ],
)
def test_admin_ids_accept_every_shape_the_env_can_deliver(given, expected) -> None:
    """pydantic-settings JSON-parses env values, so the type here varies by input."""
    assert settings(admin_telegram_ids=given).admin_telegram_ids == expected


@pytest.mark.parametrize("bad", ["abc", "111,abc", True, "-5", "0", "000000000"])
def test_non_numeric_or_invalid_admin_ids_are_refused(bad) -> None:
    with pytest.raises(ValidationError):
        settings(admin_telegram_ids=bad)


def test_the_env_example_placeholder_is_refused_with_a_useful_message() -> None:
    """000000000 is not valid JSON either, which used to surface as a raw traceback."""
    with pytest.raises(ValidationError, match="userinfobot"):
        settings(admin_telegram_ids="000000000")


def test_admin_ids_field_is_a_plain_string_so_it_is_never_json_decoded() -> None:
    """Guards the reason this field is not typed as a tuple."""
    assert Settings.model_fields["admin_telegram_ids_raw"].annotation is str


def test_is_admin_checks_membership() -> None:
    config = settings(admin_telegram_ids="111,222")
    assert config.is_admin(111) and config.is_admin(222)
    assert not config.is_admin(333)


# ---------------- secrets ----------------


def test_token_is_not_exposed_by_repr_or_str() -> None:
    config = settings()
    assert "AAFakeTokenForTests" not in repr(config)
    assert "AAFakeTokenForTests" not in str(config)
    assert "<hidden>" in repr(config)


def test_token_is_still_retrievable_deliberately() -> None:
    assert settings().telegram_bot_token.get_secret_value().startswith("123456789:")


# ---------------- comfyui ----------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert settings(comfyui_host=host).comfyui_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "comfy.example.com"])
def test_non_loopback_host_is_refused(host: str) -> None:
    """Pointing at a non-local ComfyUI must be a deliberate act, not a typo."""
    with pytest.raises(ValidationError, match="loopback"):
        settings(comfyui_host=host)


def test_urls_are_derived_from_host_and_port() -> None:
    config = settings(comfyui_port=9999)
    assert config.comfyui_base_url == "http://127.0.0.1:9999"
    assert config.comfyui_ws_url == "ws://127.0.0.1:9999/ws"


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_invalid_ports_are_refused(port: int) -> None:
    with pytest.raises(ValidationError):
        settings(comfyui_port=port)


# ---------------- paths ----------------


def test_relative_paths_resolve_against_the_project_root_not_the_cwd() -> None:
    """Running from another directory must not scatter databases and logs."""
    config = settings(database_path="data/app.db", output_dir="outputs")
    assert config.database_path == PROJECT_ROOT / "data" / "app.db"
    assert config.output_dir == PROJECT_ROOT / "outputs"


def test_absolute_paths_are_left_alone(tmp_path: Path) -> None:
    assert settings(output_dir=str(tmp_path)).output_dir == tmp_path


def test_ensure_directories_creates_only_our_own(tmp_path: Path) -> None:
    config = settings(
        output_dir=str(tmp_path / "out"),
        log_dir=str(tmp_path / "log"),
        database_path=str(tmp_path / "db" / "app.db"),
    )
    config.ensure_directories()
    assert (tmp_path / "out").is_dir()
    assert (tmp_path / "log").is_dir()
    assert (tmp_path / "db").is_dir()


# ---------------- required values ----------------


def test_missing_token_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, comfyui_output_dir=".")


def test_quota_and_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        settings(default_daily_quota=-1)
    with pytest.raises(ValidationError):
        settings(job_timeout_seconds=5)
    assert settings(default_daily_quota=0).default_daily_quota == 0
