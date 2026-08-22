"""Typed application configuration, loaded once from the environment / .env.

Every tunable value in the application comes from here. Nothing reads os.environ
directly, and no path, host, or limit is hardcoded elsewhere.
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- Telegram ----------
    telegram_bot_token: SecretStr = Field(
        ..., description="From @BotFather. Wrapped in SecretStr so it cannot be logged."
    )
    admin_telegram_ids: tuple[int, ...] = Field(
        default=(), description="Numeric Telegram user IDs with admin rights."
    )

    # ---------- ComfyUI ----------
    comfyui_host: str = "127.0.0.1"
    comfyui_port: int = Field(default=8188, ge=1, le=65535)
    comfyui_output_dir: Path

    # ---------- Local paths ----------
    database_path: Path = Path("data/app.db")
    workflow_dir: Path = Path("workflows")
    output_dir: Path = Path("outputs")
    log_dir: Path = Path("logs")

    # ---------- Generation ----------
    default_workflow: str = "txt2img_h3_plate"
    default_daily_quota: int = Field(default=10, ge=0)
    job_timeout_seconds: int = Field(default=1800, ge=30)

    # ---------- Optional, not needed before v0.3 ----------
    anthropic_api_key: SecretStr | None = None

    log_level: str = "INFO"

    # ---------------- validation ----------------

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """Accept every shape the environment can deliver.

        pydantic-settings JSON-parses env values for complex types, so a single id
        (`ADMIN_TELEGRAM_IDS=12345`) arrives here as an int and a JSON list arrives as
        a list, while `111,222` fails to parse and arrives as the raw string.
        """
        if isinstance(value, bool):
            raise ValueError("admin_telegram_ids must be Telegram user IDs")
        if isinstance(value, int):
            return (value,)
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(" ", ",").split(",")]
            try:
                return tuple(int(p) for p in parts if p)
            except ValueError:
                raise ValueError(
                    "admin_telegram_ids must be numeric Telegram user IDs, "
                    "comma separated (get yours from @userinfobot)"
                ) from None
        if isinstance(value, (list, tuple, set)):
            return tuple(int(item) for item in value)
        return value

    @field_validator("admin_telegram_ids")
    @classmethod
    def _admin_ids_positive(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        bad = [i for i in value if i <= 0]
        if bad:
            raise ValueError(f"admin_telegram_ids must be positive Telegram user IDs, got {bad}")
        return value

    @field_validator("database_path", "workflow_dir", "output_dir", "log_dir")
    @classmethod
    def _resolve_local(cls, value: Path) -> Path:
        """Relative paths are interpreted against the project root, not the cwd."""
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @model_validator(mode="after")
    def _check_loopback(self) -> Settings:
        if self.comfyui_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"comfyui_host is {self.comfyui_host!r}. This project expects ComfyUI on "
                "loopback. Pointing it elsewhere exposes generation to the network; "
                "change this deliberately, not by accident."
            )
        return self

    # ---------------- derived ----------------

    @property
    def comfyui_base_url(self) -> str:
        return f"http://{self.comfyui_host}:{self.comfyui_port}"

    @property
    def comfyui_ws_url(self) -> str:
        return f"ws://{self.comfyui_host}:{self.comfyui_port}/ws"

    def is_admin(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.admin_telegram_ids

    def ensure_directories(self) -> None:
        """Create the local directories this application owns. Never touches ComfyUI's."""
        for path in (self.output_dir, self.log_dir, self.database_path.parent):
            path.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:  # keep secrets out of tracebacks and reprs
        return (
            f"Settings(comfyui={self.comfyui_base_url}, workflow_dir={self.workflow_dir}, "
            f"admins={len(self.admin_telegram_ids)}, token=<hidden>)"
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Raises ValidationError if .env is incomplete."""
    return Settings()  # type: ignore[call-arg]
