"""Runtime configuration.

Every setting is read from the environment with the ``NICANAV_`` prefix so the
same image runs locally, in the nightly pipeline and on the Hetzner box with
nothing but a different ``.env``.  Secrets are never defaulted to a real value —
see ``infra/.env.example`` for the template that stays *outside* the repo once
filled in.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Process-wide settings, loaded once via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="NICANAV_",
        env_file=os.environ.get("NICANAV_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- services -------------------------------------------------------- #
    valhalla_url: str = "http://valhalla:8002"
    meili_url: str = "http://meilisearch:7700"
    meili_key: str = ""
    meili_index: str = "nicanav"
    database_url: str = "postgresql://nicanav:nicanav@postgis:5432/nicanav"

    # --- filesystem ------------------------------------------------------ #
    data_dir: Path = REPO_ROOT / "data"
    tiles_dir: Path = REPO_ROOT / "data" / "tiles"

    # --- public surface -------------------------------------------------- #
    public_base_url: str = "http://localhost:8080"
    tiles_base_url: str = "/tiles"
    cors_origins: str = "*"
    admin_user: str = "admin"
    admin_password: str = ""
    rate_limit_route: str = "60/minute"
    rate_limit_search: str = "300/minute"
    rate_limit_report: str = "20/minute"

    # --- third-party tokens (optional; features degrade without them) ----- #
    mapillary_token: str = ""

    # --- behaviour ------------------------------------------------------- #
    default_language: str = "es-ES"
    log_level: str = "INFO"
    debug: bool = False

    @field_validator("data_dir", "tiles_dir", mode="before")
    @classmethod
    def _expand(cls, value: str | Path) -> Path:
        return Path(str(value)).expanduser()

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def meili_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.meili_key}"} if self.meili_key else {}


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton.  Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
