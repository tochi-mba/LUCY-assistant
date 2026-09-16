"""Application configuration.

Every knob is an environment variable prefixed ``HELLO_``. Unknown variables under
the prefix are rejected rather than ignored.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "HELLO_"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]

# Parity looks for these string literals in source when this tree becomes a sibling repo.
ROUTES = ("/healthy", "/ready")


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """The complete runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_name: str = "hello"
    environment: str = "local"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    host: str = "127.0.0.1"
    port: PositiveInt = 8090

    keyring_jwks_url: str = "http://127.0.0.1:8001/.well-known/jwks.json"
    keyring_issuer: str = "http://127.0.0.1:8001"
    audience: str = "hello"
    jwks_cache_seconds: PositiveFloat = 3_600.0
    jwks_min_refetch_seconds: PositiveFloat = 30.0
    keyring_timeout_seconds: PositiveFloat = 5.0

    @model_validator(mode="after")
    def _audience_is_usable(self) -> Self:
        value = self.audience
        if not value or value.strip() != value or "." in value:
            msg = "HELLO_AUDIENCE must be non-empty, trimmed, and contain no dot"
            raise ValueError(msg)
        return self


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Refuse unknown ``HELLO_*`` variables so a typo fails at startup."""
    known = {ENV_PREFIX + name.upper() for name in Settings.model_fields}
    source = environ if environ is not None else os.environ
    unknown = sorted(key for key in source if key.startswith(ENV_PREFIX) and key not in known)
    if unknown:
        msg = "unknown environment variables: " + ", ".join(unknown)
        raise RuntimeError(msg)


def load_settings() -> Settings:
    """Load settings and refuse unknown env vars under the prefix."""
    check_for_unknown_env_vars()
    return Settings()
