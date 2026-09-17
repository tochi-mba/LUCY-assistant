"""Configuration for the hub.

Every knob is an environment variable prefixed ``LUCY_``, and an unknown one under that
prefix is a startup error rather than a silently ignored typo. That rule matters more here
than in a leaf service: the hub holds the base URL of every sibling, and a misspelled
``LUCY_KEYRNIG_BASE_URL`` would otherwise start a process that looks healthy right up until
the first token needs verifying.

Configuration is a fact about the machine. A fact about a *person* -- which model they
prefer, how much context they want spent on memory, whether a destructive tool may run
without asking -- belongs in settings-api under the ``lucy`` namespace, never here.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "LUCY_"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]

# Parity looks for these literals in source. Liveness and readiness are different questions
# and are answered by different routes; see docs/architecture.md.
ROUTES = ("/healthy", "/ready")

# The `lucy` command shares this prefix, deliberately: one product, one namespace, so a
# person exports LUCY_TOKEN once and both halves understand it. They are not settings of
# this process, and refusing them as typos would make the server crash in exactly the
# environment the client's own help tells people to create. A test pins this list against
# the names the CLI actually reads, so the two cannot drift apart.
CLIENT_VARIABLES = frozenset(
    {ENV_PREFIX + name for name in ("URL", "TOKEN", "CONFIG", "FAMILY_ROOT")}
)


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class ExtraSibling(BaseModel):
    """One operator-local service the published family does not name.

    Capability packs look these up by product id (``media``, not a repository name).
    An empty ``base_url`` is the same as omitting the entry.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    audience: str = ""
    documentation: str = ""
    title: str = ""
    instructions: str = ""
    checks: tuple[str, ...] = ()


class Settings(BaseSettings):
    """The complete runtime configuration of the hub."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_name: str = "lucy"
    environment: str = "local"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    host: str = "127.0.0.1"
    port: PositiveInt = 8000

    database_path: str = "var/lucy.sqlite3"
    """The one file every conversation lives in.

    A ``str`` and not a ``Path``, matching memory-api, because ``:memory:`` is a sqlite
    sentinel rather than a filename: expanding and resolving it -- which is what a ``Path``
    field invites -- turns an in-memory database into a file literally called ``:memory:``
    in the working directory, and the tests that rely on isolation would quietly start
    sharing one.

    The directory is created on first open, so a deployment configures the file it wants
    rather than a directory it has to remember to make first.
    """

    # Identity. The audience is the hub's name in keyring's KEYRING_SERVICE_TOKENS, and it
    # must equal the audience_prefix settings-api grants it; a mismatch fails closed and
    # looks exactly like a correctly configured service whose every call is a 401.
    audience: str = "lucy-api"
    keyring_base_url: str = "http://127.0.0.1:8001"
    keyring_jwks_url: str = "http://127.0.0.1:8001/.well-known/jwks.json"
    keyring_issuer: str = "http://127.0.0.1:8001"
    keyring_service_token: str = ""

    # Model credentials are optional at boot so a new installation can still expose setup
    # and readiness. They never enter a prompt, event or log; the model registry consumes
    # them only while it constructs an HTTP client.
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # The rest of the family. Each is a base URL only; what the hub does with them lives in
    # a capability pack, and a pack whose service is unreachable is absent from the model's
    # tools rather than an error in somebody's turn.
    user_api_base_url: str = "http://127.0.0.1:8002"
    settings_api_base_url: str = "http://127.0.0.1:8003"
    settings_api_token: str = ""
    persona_api_base_url: str = "http://127.0.0.1:8004"
    web_search_base_url: str = "http://127.0.0.1:8006"
    spotify_api_base_url: str = "http://127.0.0.1:8007"
    environments_api_base_url: str = "http://127.0.0.1:8008"
    memory_api_base_url: str = "http://127.0.0.1:8009"

    extra_services: dict[str, ExtraSibling] = Field(default_factory=dict)
    """Operator-local siblings, keyed by capability id. Empty means none are wired."""

    # Timeouts and caches.
    jwks_cache_seconds: PositiveFloat = 3_600.0
    jwks_min_refetch_seconds: PositiveFloat = 30.0
    http_timeout_seconds: PositiveFloat = 10.0

    @model_validator(mode="after")
    def _audience_is_usable(self) -> Self:
        value = self.audience
        if not value or value.strip() != value:
            msg = "LUCY_AUDIENCE must be non-empty and carry no leading or trailing space"
            raise ValueError(msg)
        return self

    def extra(self, capability: str) -> ExtraSibling | None:
        """The configured sibling for a capability, or none when it is not wired."""
        row = self.extra_services.get(capability)
        if row is None or not row.base_url.strip():
            return None
        return row


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Refuse unknown ``LUCY_*`` variables so a typo fails at startup, not at first use.

    The `lucy` command's own variables are known, not unknown. See ``CLIENT_VARIABLES``.
    """
    known = {ENV_PREFIX + name.upper() for name in Settings.model_fields} | CLIENT_VARIABLES
    source = environ if environ is not None else os.environ
    unknown = sorted(key for key in source if key.startswith(ENV_PREFIX) and key not in known)
    if unknown:
        msg = "unknown environment variables: " + ", ".join(unknown)
        raise RuntimeError(msg)


def load_settings() -> Settings:
    """Load settings, refusing unknown variables under the prefix first."""
    check_for_unknown_env_vars()
    return Settings()
