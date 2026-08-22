"""Entry point.

    python run_bot.py

Reads configuration from .env, checks it before touching the network, and starts the
bot with long polling. Stop it with Ctrl+C.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import ValidationError  # noqa: E402
from pydantic_settings import SettingsError  # noqa: E402

from app.bot.application import GenerationServer  # noqa: E402
from app.config.settings import PROJECT_ROOT, get_settings  # noqa: E402
from app.utils.logging import configure_logging, get_logger  # noqa: E402


def main() -> int:
    try:
        settings = get_settings()
    except (ValidationError, SettingsError) as exc:
        # SettingsError covers values pydantic-settings could not even parse, which
        # would otherwise surface as a raw traceback about JSON.
        print("Configuration is incomplete or invalid.\n")
        errors = exc.errors() if isinstance(exc, ValidationError) else []
        for error in errors:
            field = ".".join(str(part) for part in error["loc"]) or "(config)"
            print(f"  {field}: {error['msg']}")
        if not errors:
            print(f"  {exc}")
        env_file = PROJECT_ROOT / ".env"
        print(
            f"\nEdit {env_file}."
            + ("" if env_file.exists() else "  It does not exist yet - copy .env.example to .env.")
        )
        return 2

    configure_logging(settings.log_dir, settings.log_level)
    log = get_logger("run_bot")

    if not settings.admin_telegram_ids:
        # Without an admin id nobody can use the bot, which is a confusing way to fail.
        print("ADMIN_TELEGRAM_IDS is empty, so no one would be authorised.")
        print("Put your numeric Telegram id in .env (get it from @userinfobot).")
        return 2

    log.info(
        "starting",
        comfyui=settings.comfyui_base_url,
        workflow=settings.default_workflow,
        admins=len(settings.admin_telegram_ids),
    )

    try:
        GenerationServer(settings).run()
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        log.error("startup.failed", error=str(exc))
        print(f"\nCould not start: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
