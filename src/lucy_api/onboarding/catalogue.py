"""Small, explicit manifests grounded in the siblings' own setup documentation.

The family does not yet publish authenticated setup manifests. These adapters name the
existing readiness route and documentation, and say when an operator or Keyring session
is needed. They deliberately do not ingest arbitrary OpenAPI descriptions or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.onboarding.models import ConnectionState, SetupAction

if TYPE_CHECKING:
    from lucy_api.core.config import ExtraSibling, Settings


@dataclass(frozen=True, slots=True)
class SetupManifest:
    """Only the deployment owns these URLs and the names accepted from its probes."""

    id: str
    title: str
    base_url: str
    documentation: str
    instructions: str
    checks: tuple[str, ...]
    required: bool = False
    connection_state: ConnectionState = "not_required"

    def actions(self) -> list[SetupAction]:
        """Expose guidance without presenting an API route as a browser sign-in page."""
        return [
            SetupAction(kind="operator", label="Setup steps", description=self.instructions),
            SetupAction(
                kind="documentation",
                label="Read setup documentation",
                description="The service's published setup instructions.",
                url=self.documentation,
            ),
        ]


REPOSITORIES = "https://github.com/tochi-mba/"
PRIVATE_DOCS = REPOSITORIES + "LUCY-assistant/blob/main/docs/private-repos.md"

_EXTRA_INSTRUCTIONS = (
    "Configure this operator-local service and identity verification. Lucy cannot "
    "yet inspect your connection."
)


def _extra_manifest(capability: str, sibling: ExtraSibling) -> SetupManifest:
    """One setup card for a capability only this machine has.

    Everything specific to it -- its name, its documentation, the sentence a person reads
    and the checks its readiness reports -- comes from the operator's own configuration.
    The hub special-cases nothing, which is the point: a branch here reading
    ``if capability == "..."`` would name a private service in a public file, and the next
    private service would need a second branch. See ADR-0011.

    The fallbacks are deliberately generic. A capability that supplies nothing still gets a
    usable card rather than a blank one, and an operator who wants a better card writes a
    better ``instructions`` rather than patching the hub.
    """
    label = sibling.title or capability.replace("-", " ").replace("_", " ").title()
    return SetupManifest(
        id=capability,
        title=label,
        base_url=sibling.base_url,
        documentation=sibling.documentation or PRIVATE_DOCS,
        instructions=sibling.instructions or _EXTRA_INSTRUCTIONS,
        checks=sibling.checks or ("ready",),
        connection_state="unknown",
    )


def extra_manifests(settings: Settings) -> tuple[SetupManifest, ...]:
    """Capabilities wired only on this machine, in the order the operator listed them."""
    return tuple(
        _extra_manifest(capability, sibling)
        for capability, sibling in settings.extra_services.items()
        if sibling.base_url.strip()
    )


def manifests(settings: Settings) -> tuple[SetupManifest, ...]:
    """Keep published identifiers stable; extras sit between notes and research."""
    head = (
        SetupManifest(
            id="identity",
            title="Identity",
            base_url=settings.keyring_base_url,
            documentation=REPOSITORIES + "Keyring-api#readme",
            instructions=(
                "Configure the identity service, unseal its credential vault, and create an "
                "account. Lucy verifies your account token but cannot yet start browser sign-in."
            ),
            checks=("accounts", "vault", "connections"),
            required=True,
        ),
        SetupManifest(
            id="facts",
            title="Personal facts",
            base_url=settings.user_api_base_url,
            documentation=REPOSITORIES + "User-api#readme",
            instructions=(
                "Configure the facts database and identity verification. Personal facts need "
                "no additional provider account."
            ),
            checks=("database", "keyring"),
        ),
        SetupManifest(
            id="preferences",
            title="Preferences",
            base_url=settings.settings_api_base_url,
            documentation=REPOSITORIES + "Settings-api#readme",
            instructions=(
                "Configure the settings database, identity verification and Lucy's namespace "
                "grant. Preferences need no additional provider account."
            ),
            checks=("database", "keyring", "catalogue", "policy"),
        ),
        SetupManifest(
            id="notes",
            title="Persona notes",
            base_url=settings.persona_api_base_url,
            documentation=REPOSITORIES + "Persona-api#readme",
            instructions=(
                "Configure the persona database and identity verification. Behaviour notes "
                "need no additional provider account."
            ),
            checks=("database", "keyring", "settings"),
        ),
    )
    tail = (
        SetupManifest(
            id="research",
            title="Research",
            base_url=settings.web_search_base_url,
            documentation=REPOSITORIES + "Web-search-api/blob/main/docs/keyring.md",
            instructions=(
                "Choose a model provider. Local model runtimes need no provider credential; "
                "remote provider keys belong to your profile in the identity vault. Lucy "
                "cannot yet inspect your connected providers."
            ),
            checks=("keyring", "browser", "providers", "models", "settings"),
            connection_state="unknown",
        ),
        SetupManifest(
            id="music",
            title="Spotify music",
            base_url=settings.spotify_api_base_url,
            documentation=REPOSITORIES + "Spotify-api#connecting-a-spotify-account",
            instructions=(
                "Optional: configure Spotify's OAuth provider in Keyring. Sign in to Keyring "
                "with your own account and use POST /v1/profiles/{profile}/connections/spotify/"
                "authorize with that Keyring session, then open the returned consent URL. "
                "Keyring stores and refreshes the connection for your account and profile; "
                "Lucy cannot yet start this flow or inspect its state."
            ),
            checks=("spotify",),
            connection_state="unknown",
        ),
        SetupManifest(
            id="workspace",
            title="Workspace",
            base_url=settings.environments_api_base_url,
            documentation=REPOSITORIES + "Environments-api#readme",
            instructions=(
                "Configure the sandbox backend, storage and identity verification. "
                "Workspace access needs no additional provider sign-in."
            ),
            checks=("keyring", "storage", "backend"),
        ),
        SetupManifest(
            id="memory",
            title="Memory",
            base_url=settings.memory_api_base_url,
            documentation=REPOSITORIES + "Memory-api#readme",
            instructions=(
                "Configure the memory database and identity verification. Memory needs no "
                "additional provider account. It can be skipped, and Lucy still works -- it "
                "simply starts each conversation knowing only what is in front of it."
            ),
            checks=("database", "keyring"),
        ),
    )
    return (*head, *extra_manifests(settings), *tail)
