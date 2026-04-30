"""Central configuration — reads from env vars and ~/.mailtrim/config.toml."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default data directory: ~/.mailtrim/
DATA_DIR = Path(os.environ.get("MAILTRIM_DIR", Path.home() / ".mailtrim"))
DB_PATH = DATA_DIR / "mailtrim.db"
CREDENTIALS_PATH = DATA_DIR / "credentials.json"  # OAuth client secret (downloaded from GCP)
TOKEN_PATH = DATA_DIR / "token.json"  # OAuth access/refresh token (generated)
UNDO_LOG_DIR = DATA_DIR / "undo_logs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAILTRIM_",
        env_file=str(DATA_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    ai_model: str = "claude-sonnet-4-6"

    # Gmail OAuth scopes
    gmail_scopes: list[str] = Field(
        default=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    )

    # Behaviour
    dry_run: bool = False  # Global dry-run override
    undo_window_days: int = 30  # How long undo logs are kept
    avoidance_view_threshold: int = 3  # Views before an email is "avoided"
    follow_up_default_days: int = 3  # Default follow-up reminder window

    # Rate limiting
    gmail_batch_size: int = 50  # Max messages per Gmail batch request
    ai_max_classify_batch: int = 20  # Emails per AI classification call


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Restrict the data directory so other local users cannot browse it.
        # Credentials and tokens stored here are sensitive.
        DATA_DIR.chmod(0o700)
        UNDO_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _settings = Settings()
    return _settings
