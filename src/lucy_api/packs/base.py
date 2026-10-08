"""What a capability is, and why the model never learns the name of a service.

A **pack** is one thing a person can do, named the way they would name it: `music`,
`research`, `workspace`, `notes`. Behind it might be one service, three, or none. That
indirection is not decoration, it is the rule the whole tool layer is built on.

**The model never sees a service.** It does not know there is a Spotify API, it does not
know a port number, and it cannot be talked into calling one. What it sees is `music.play`
with a description written for it. A tool surface generated from an OpenAPI document would
leak all three, and would also be unable to say the thing a model most needs to hear, which
is when *not* to call something.

**Operations are workflow-shaped, not route-shaped.** `music.find_and_play` fans out to
keyring and a provider internally. Exposing `get_track`, `get_album` and `get_artist`
instead makes the model do the joining, in context, one round trip at a time -- which is
precisely the cost weftai's plans exist to avoid.

## Availability is eight states, not a boolean

The tempting model is connected-or-not. It is wrong in a way people feel immediately:
somebody who started connecting Spotify and closed the tab is not "not connected", they are
`pending`, and an assistant that cannot tell the difference will either nag them to start
again or claim they are set up. `insufficient_scope` is the one a partial-consent screen
forces on you, and the token response's granted scopes are authoritative -- never the ones
that were requested.

## Two audiences, two answers

For Lucy's own loop an unconnected capability is **absent**: it is not in the registry, so
the model cannot call it, cannot half-call it, and cannot apologise for it.

Over MCP the tool stays **listed**, with a description saying it needs connecting. A
vanished tool gives a client that cached the list no way back, and a model that cannot see
a tool cannot explain what it would be able to do if the person connected it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation

    from lucy_api.context.types import Trust
    from lucy_api.packs.context import PackContext


class State(StrEnum):
    """Where a capability stands for one person, right now."""

    ready = "ready"
    """Usable. Its operations are in the registry this turn."""

    not_connected = "not_connected"
    """No credential. The model is told it exists and how to offer setting it up."""

    pending = "pending"
    """A consent link was handed over and never came back. The state everyone forgets."""

    expired = "expired"
    """The grant lapsed. A reconnect, not a first connection, and it should say so."""

    insufficient_scope = "insufficient_scope"
    """Partial consent. Name the missing scope; the granted set is authoritative."""

    not_configured = "not_configured"
    """The operator never deployed it. Not the person's problem and not shown to them."""

    unavailable = "unavailable"
    """It is down. Absent for the model this turn, still listed over MCP."""

    disabled = "disabled"
    """The person turned it off. Hidden, and the model is told so it stops offering."""


READY_STATES = frozenset({State.ready})
"""The only state whose operations are bound for the model. Everything else is absent."""

OFFERABLE_STATES = frozenset(
    {State.not_connected, State.pending, State.expired, State.insufficient_scope}
)
"""States where setup is worth offering. `disabled` is not: they already said no."""


@dataclass(frozen=True, slots=True)
class Availability:
    """The answer a probe gives, and the sentence a person would want with it."""

    state: State
    detail: str = ""
    missing_scopes: tuple[str, ...] = ()
    connect_url: str = ""
    checked_at: float = 0.0

    @property
    def usable(self) -> bool:
        return self.state in READY_STATES

    @property
    def offer_setup(self) -> bool:
        return self.state in OFFERABLE_STATES


@dataclass(frozen=True, slots=True)
class Permission:
    """One thing a person reasons about granting, which is never one operation.

    Nobody wants to approve forty operations one at a time, and nobody has any idea what
    `workspace.shell` means on its own. "Run commands in your workspace" is the unit a
    person can actually answer, so it is the unit that is asked about and recorded.
    """

    id: str
    title: str
    description: str
    risk: str
    covers: tuple[str, ...]
    outward: bool = False
    """True when other people will see the effect. auto cannot skip asking about those."""
    tally: str = ""
    """The input field that tells one call of this permission from another -- `role` for a
    helper -- so a card asking about several in one plan can count them: "researcher x2,
    reviewer x3". Empty, the card counts the calls and names none."""
    each_call: Callable[[Mapping[str, object]], bool] | None = None
    """Whether a call, by its arguments, needs the person's yes to that call alone: neither
    `auto` nor a standing grant covers it. A setting declared `with_approval` is the case --
    "you can manage my settings" is not approval to lower a memory floor."""
    refuses: Callable[[Mapping[str, object]], str] | None = None
    """A sentence when a call, by its arguments, can never run -- so it is refused before
    anyone is asked. A setting declared `never` is the case: the card used to be shown, the
    person said yes, and the write was refused anyway, a question whose yes changed nothing.
    Empty means the ordinary gate decides."""


@dataclass(frozen=True, slots=True)
class SetupStep:
    """One thing that has to happen before a capability works."""

    id: str
    kind: str
    title: str
    description: str
    store: str = "keyring"
    required: bool = True


@dataclass(frozen=True, slots=True)
class SetupPlan:
    """How a person connects this capability, rendered by whoever is asking."""

    summary: str
    steps: tuple[SetupStep, ...] = ()
    docs_url: str = ""
    estimated_seconds: int = 60


class CapabilityPack(Protocol):
    """One capability, as the rest of Lucy sees it."""

    id: str
    title: str
    summary: str

    @property
    def docs(self) -> str | Path | None:
        """Markdown written for the model, loaded only when it asks.

        A string is the in-process manual. A path is a file the operator dropped beside
        the pack. ``None`` falls back to the one-line summary, which is how a capability
        that has not yet written its manual still answers ``help.docs``.
        """
        ...

    def operations(self, context: PackContext) -> Sequence[AnyOperation]: ...

    def permissions(self) -> Sequence[Permission]: ...

    async def probe(self, context: PackContext) -> Availability: ...

    def setup(self) -> SetupPlan | None: ...

    def result_trust(self, operation: str, data: object) -> Trust:
        """How far the result of one of this pack's operations can be trusted.

        Decided by where the result came from, never by what it says. `observed` is what
        Lucy's own machinery wrote, where no outsider chose a word of it. Anything a page, a
        file, a command, a service or a helper produced is `untrusted`, and is framed to the
        model as something to weigh and not obey. The pack decides, because the pack is what
        knows where its results come from.
        """
        ...


@dataclass(frozen=True, slots=True)
class Bound:
    """One pack after probing: what it is, how it stands, and what it offers this turn."""

    pack: CapabilityPack
    availability: Availability
    operations: tuple[AnyOperation, ...] = ()

    @property
    def visible_to_model(self) -> bool:
        return self.availability.usable and bool(self.operations)


CONNECTION_REQUIRED = "connection_required"
"""The status in a tool result that means "not set up", never an HTTP error.

A model handed a 502 will apologise or retry. A model handed this will offer the person a
link. The body carries the service, the missing scopes, the connect URL, and a sentence
telling the model not to ask for a password -- which is the cheap defence against it
improvising a credential prompt.
"""


def connection_required(
    *, service: str, profile: str, connect_url: str, scopes: Sequence[str] = ()
) -> dict[str, Any]:
    """The fixed, machine-readable body a capability returns when it is not connected."""
    return {
        "status": CONNECTION_REQUIRED,
        "service": service,
        "profile": profile,
        "scopes": list(scopes),
        "connect_url": connect_url,
        "message": (
            f"{service} is not connected for profile '{profile}'. Ask the person to open "
            "the link. Do not ask them for a password or a token."
        ),
    }


@dataclass(frozen=True, slots=True)
class Catalogue:
    """Every pack this deployment has, probed for one person."""

    bound: tuple[Bound, ...] = field(default_factory=tuple)
    suggested: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    """The person's `preferred_capabilities`, carried with the probe so every caller that
    chooses what is bound this turn ranks by the same list."""

    def ready(self) -> tuple[Bound, ...]:
        return tuple(item for item in self.bound if item.visible_to_model)

    def operations(self) -> tuple[AnyOperation, ...]:
        return tuple(op for item in self.ready() for op in item.operations)

    def get(self, pack_id: str) -> Bound | None:
        return next((item for item in self.bound if item.pack.id == pack_id), None)


__all__ = [
    "CONNECTION_REQUIRED",
    "OFFERABLE_STATES",
    "READY_STATES",
    "Availability",
    "Bound",
    "CapabilityPack",
    "Catalogue",
    "Permission",
    "SetupPlan",
    "SetupStep",
    "State",
    "connection_required",
]
