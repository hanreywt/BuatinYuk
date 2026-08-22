"""Typed application configuration, loaded once from the environment / .env.

Every tunable value in the application comes from here. Nothing reads os.environ
directly, and no path, host, or limit is hardcoded elsewhere.
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import AliasChoices, Field, PrivateAttr, SecretStr, field_validator, model_validator
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
    #: Declared as a plain string on purpose. pydantic-settings JSON-decodes any field
    #: whose type is complex (tuple/list/dict), which makes ordinary spellings fail in
    #: confusing ways: `000000000` is not valid JSON, and a bare `12345` decodes to an
    #: int rather than a sequence. Taking the raw text and parsing it here accepts every
    #: reasonable spelling and produces a clear error for the rest. Read the parsed value
    #: through the `admin_telegram_ids` property.
    admin_telegram_ids_raw: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ADMIN_TELEGRAM_IDS", "admin_telegram_ids", "admin_telegram_ids_raw"
        ),
        description="Numeric Telegram user IDs with admin rights, comma separated.",
    )

    # ---------- ComfyUI ----------
    comfyui_host: str = "127.0.0.1"
    comfyui_port: int = Field(default=8188, ge=1, le=65535)
    comfyui_output_dir: Path

    # ---------- Local paths ----------
    database_path: Path = Path("data/app.db")
    workflow_dir: Path = Path("workflows")
    output_dir: Path = Path("outputs")
    #: Where images sent by users are kept. Separate from ComfyUI's own input folder so
    #: an upload is always attributable to the job and owner that produced it.
    input_dir: Path = Path("inputs")
    log_dir: Path = Path("logs")

    # ---------- Generation ----------
    default_workflow: str = "txt2img_h3_plate"
    #: Used when the request carries an image instead of text alone.
    image_workflow: str = "img2img_h3"
    #: Used when the request asks for video.
    video_workflow: str = "txt2video_h3"
    #: Used when the request carries an image and asks for video.
    image_video_workflow: str = "img2video_h3"
    default_daily_quota: int = Field(default=10, ge=0)
    job_timeout_seconds: int = Field(default=1800, ge=30)

    # ---------- Optional, not needed before v0.3 ----------
    anthropic_api_key: SecretStr | None = None

    # ---------- Local dashboard ----------
    #: Must stay on loopback: the dashboard exposes the queue and worker controls
    #: with no authentication, because nothing remote can reach it.
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8765, ge=1, le=65535)

    log_level: str = "INFO"

    #: Parsed form of `admin_telegram_ids_raw`, filled in during validation.
    _admin_ids: tuple[int, ...] = PrivateAttr(default=())

    # ---------------- validation ----------------

    @field_validator("admin_telegram_ids_raw", mode="before")
    @classmethod
    def _normalise_admin_ids(cls, value: object) -> str:
        """Accept a string, a single int, or a sequence, and reduce it to raw text."""
        if value is None:
            return ""
        if isinstance(value, bool):
            raise ValueError("ADMIN_TELEGRAM_IDS must be numeric Telegram user IDs")
        if isinstance(value, int):
            return str(value)
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(item) for item in value)
        return str(value)

    @model_validator(mode="after")
    def _parse_admin_ids(self) -> Settings:
        parts = [
            part.strip()
            for part in self.admin_telegram_ids_raw.replace(" ", ",").split(",")
            if part.strip()
        ]
        try:
            ids = tuple(int(part) for part in parts)
        except ValueError:
            raise ValueError(
                "ADMIN_TELEGRAM_IDS must be numeric Telegram user IDs, comma separated. "
                "Get yours from @userinfobot."
            ) from None

        bad = [i for i in ids if i <= 0]
        if bad:
            raise ValueError(
                f"ADMIN_TELEGRAM_IDS contains {bad}, which cannot be a Telegram user ID. "
                "Replace the placeholder with your own numeric id from @userinfobot."
            )
        self._admin_ids = ids
        return self

    @field_validator("database_path", "workflow_dir", "output_dir", "input_dir", "log_dir")
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
        if self.dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"dashboard_host is {self.dashboard_host!r}. The dashboard has no "
                "authentication and exposes the queue and worker controls, so it must "
                "stay on loopback."
            )
        return self

    # ---------------- derived ----------------

    @property
    def admin_telegram_ids(self) -> tuple[int, ...]:
        """Numeric Telegram user IDs with admin rights."""
        return self._admin_ids

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
        for path in (self.output_dir, self.input_dir, self.log_dir, self.database_path.parent):
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
