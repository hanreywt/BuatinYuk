"""The scanner that would have stopped a real token reaching a commit."""

from __future__ import annotations

import pytest

from scripts.check_secrets import KNOWN_PLACEHOLDERS, redact, scan_text


def test_detects_a_real_telegram_token() -> None:
    found = scan_text("TELEGRAM_BOT_TOKEN=999999999:AAThisIsNotARealTokenItIsATestFixture")
    assert [label for label, _ in found] == ["Telegram bot token"]


def test_ignores_the_shipped_placeholder() -> None:
    """The template must stay committable."""
    for placeholder in KNOWN_PLACEHOLDERS:
        assert scan_text(f"TELEGRAM_BOT_TOKEN={placeholder}") == []


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_detects_other_credential_shapes(secret: str) -> None:
    assert scan_text(secret)


def test_ordinary_content_is_not_flagged() -> None:
    text = "The bot uses long polling. See docs/integration-test.md, job #52, port 8188."
    assert scan_text(text) == []


def test_reporting_never_prints_a_whole_secret() -> None:
    token = "999999999:AAThisIsNotARealTokenItIsATestFixture"
    shown = redact(token)
    assert token not in shown
    assert shown.startswith("999999")
