"""Application configuration.

Every knob is an environment variable prefixed ``CODER_``. Unknown variables under the
prefix are rejected rather than ignored, so a typo fails at startup instead of being a
setting that silently never applied -- the family's rule, copied from the siblings. The
check covers the environment and ``.env`` alike, and only the prefix: ``.env`` is the
checkout's shared file, and the hub's own keys in it are not this service's business.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Annotated

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "CODER_"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]

ROUTES = ("/healthy", "/ready")


class Settings(BaseSettings):
    """The complete runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        # Not "forbid": with an env file that refuses every unprefixed key in it, so the
        # hub's own keys in the shared .env stopped the bridge starting. Typos under the
        # prefix are still refused, by `check_for_unknown_env_vars`.
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "coder-api"
    host: str = "127.0.0.1"
    port: PositiveInt = 8012

    # -- who may call: keyring tokens minted for this audience
    keyring_jwks_url: str = "http://127.0.0.1:8001/.well-known/jwks.json"
    keyring_issuer: str = "http://127.0.0.1:8001"
    audience: str = "coder-api"
    jwks_cache_seconds: PositiveFloat = 3_600.0
    jwks_min_refetch_seconds: PositiveFloat = 30.0
    keyring_timeout_seconds: PositiveFloat = 5.0

    # -- the CLI, and what one task may spend
    claude_command: list[str] = Field(default_factory=lambda: ["claude"])
    """How the ``claude`` binary is invoked. A list so the tests can point it at a fake
    (``[sys.executable, "fake_claude.py"]``) without a shell in between."""

    max_live_tasks: PositiveInt = 2
    """How many Claude Code sessions run at once; the rest queue. The owner chose two."""

    turn_budget_usd: PositiveFloat = 3.0
    """`--max-budget-usd` for each turn. The CLI ends the turn honestly when it is spent
    (`subtype: error_max_budget_usd`), which this service reports as the task failing.
    Three dollars, because the first turn of a session builds its prompt cache: creating a
    one-line file cost $0.82 on the live smoke, and a cap of one would have made every
    first turn a coin toss."""

    turn_timeout_seconds: PositiveFloat = 2_700.0
    """The bridge's own wall clock per turn: 45 minutes, then the process tree is killed.
    The CLI has no `--max-turns` in this version, so the clock is the backstop."""

    tail_chars_max: PositiveInt = 20_000
    """The largest transcript tail one read may ask for."""

    var_dir: str = "var/coder"
    """Task rows (SQLite) and one JSONL transcript per task live here; gitignored."""


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Refuse unknown ``CODER_*`` variables so a typo fails at startup."""
    known = {ENV_PREFIX + name.upper() for name in Settings.model_fields}
    source = environ if environ is not None else os.environ
    unknown = sorted(key for key in source if key.startswith(ENV_PREFIX) and key not in known)
    if unknown:
        msg = "unknown environment variables: " + ", ".join(unknown)
        raise RuntimeError(msg)


def load_settings() -> Settings:
    """Load settings and refuse unknown variables under the prefix, in the environment or `.env`."""
    from_file = {key: value or "" for key, value in dotenv_values(".env").items()}
    check_for_unknown_env_vars({**from_file, **os.environ})
    return Settings()
