"""Structured logging.

Console output stays readable while developing; the file handler gets JSON lines so
logs can be grepped and parsed later. Secrets are redacted centrally, so a stray
`log.info("...", token=...)` cannot leak a credential to disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

import structlog

_SENSITIVE_KEYS = {
    "token", "telegram_bot_token", "bot_token", "api_key", "anthropic_api_key",
    "secret", "password", "invite_code", "authorization",
}

# Telegram bot tokens look like 123456789:AA... - catch them even inside a message.
_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
REDACTED = "<redacted>"


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        elif isinstance(event_dict[key], str):
            event_dict[key] = _TOKEN_PATTERN.sub(REDACTED, event_dict[key])
    return event_dict


def configure_logging(log_dir: Path, level: str = "INFO") -> None:
    """Idempotent. Safe to call from the bot, the MCP server, or a script."""
    log_dir.mkdir(parents=True, exist_ok=True)

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _redact,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    console = logging.StreamHandler()
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=shared,
        )
    )
    root.addHandler(console)

    # 5 MB x 5 files is plenty for a single-machine service.
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared,
        )
    )
    root.addHandler(file_handler)

    # httpx logs every request line at INFO; too noisy for a polling bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
