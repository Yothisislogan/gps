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
from urllib.parse import quote

from pydantic import field_validator, model_validator
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

    # --- database ---------------------------------------------------------- #
    # The DSN is composed from parts rather than passed in whole, because a
    # generated password routinely contains characters that a URL cannot carry
    # raw: `openssl rand -base64 32` emits `/` and `+`, and a `/` silently
    # truncates a postgresql:// URL at the database name. Composing here means
    # the password is percent-encoded exactly once, by code that knows it is a
    # password. Set NICANAV_DATABASE_URL explicitly to override.
    database_url: str = ""
    pg_host: str = "postgis"
    pg_port: int = 5432
    pg_db: str = "nicanav"
    pg_user: str = "nicanav"
    pg_password: str = "nicanav"

    # --- filesystem ------------------------------------------------------ #
    data_dir: Path = REPO_ROOT / "data"
    tiles_dir: Path = REPO_ROOT / "data" / "tiles"

    # --- public surface -------------------------------------------------- #
    public_base_url: str = "http://localhost:8400"
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

    @model_validator(mode="after")
    def _compose_database_url(self) -> Settings:
        """Fill in ``database_url`` from the parts when it was not given."""
        if not self.database_url:
            password = quote(self.pg_password, safe="")
            user = quote(self.pg_user, safe="")
            self.database_url = (
                f"postgresql://{user}:{password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"
            )
        return self

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
