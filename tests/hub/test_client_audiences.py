"""Every audience the hub asks for must be one keyring will mint for it.

Keyring mints whatever it is asked for, and each sibling pins the `aud` claim it will accept,
exactly. So a wrong audience is refused by the sibling rather than by keyring, and the only
place it shows is that sibling's own log -- which nobody reads when the hub is the thing being
worked on.

It happened: the hub asked for `persona-api`, persona pins `persona`, and every turn ever
served carried `token_rejected reason=audience` in persona's log and no persona in its prompt.
The hub reported nothing, because a persona that cannot be fetched is a persona left out, which
is the correct behaviour for a sibling that is down.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from lucy_api.clients.environments import AUDIENCE as ENVIRONMENTS
from lucy_api.clients.keyring import AUDIENCE as KEYRING
from lucy_api.clients.live_feeds import PERSONA_AUDIENCE
from lucy_api.clients.memory import AUDIENCE as MEMORY
from lucy_api.clients.search import AUDIENCE as SEARCH
from lucy_api.clients.settings import AUDIENCE as SETTINGS
from lucy_api.clients.spotify import AUDIENCE as SPOTIFY
from lucy_api.clients.user import AUDIENCE as USER

META_ROOT = Path(__file__).resolve().parents[2]

ASKED_FOR = {
    "environments": ENVIRONMENTS,
    "memory": MEMORY,
    "persona": PERSONA_AUDIENCE,
    "search": SEARCH,
    "settings": SETTINGS,
    "spotify": SPOTIFY,
    "user": USER,
}
"""Every audience a client asks keyring to mint, by the sibling it is for.

Keyring is not here: the hub authenticates to it rather than exchanging for it.
"""


def allowlist() -> tuple[str, ...]:
    """`LUCY_EXCHANGE_AUDIENCES` read out of the generator, without importing it."""
    source = (META_ROOT / "scripts" / "genenv.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        targets = getattr(node, "targets", []) or ([node.target] if hasattr(node, "target") else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "LUCY_EXCHANGE_AUDIENCES":
                return tuple(ast.literal_eval(node.value))
    msg = "LUCY_EXCHANGE_AUDIENCES is not assigned at the top level of scripts/genenv.py"
    raise AssertionError(msg)


@pytest.mark.parametrize("sibling", sorted(ASKED_FOR))
def test_every_audience_a_client_asks_for_is_one_keyring_will_mint(sibling: str) -> None:
    """The allowlist is deliberate -- adding a sibling does not silently give the hub authority
    to call it -- so an audience missing from it is a call that cannot be made."""
    assert ASKED_FOR[sibling] in allowlist()


def test_keyring_is_not_an_exchange_audience() -> None:
    """The hub authenticates to keyring; it does not exchange for it. An entry would be a grant
    of authority nobody asked for."""
    assert KEYRING not in allowlist()


def test_the_persona_audience_is_not_the_service_name() -> None:
    """The bug itself, named. `persona-api` is what the service is called; `persona` is what it
    pins -- see `Persona-api/src/persona_api/core/config.py`, which says so in as many words."""
    assert PERSONA_AUDIENCE == "persona"
