"""Every settings namespace the hub resolves must be one it has been granted.

settings-api grants namespaces per consuming service, and a namespace a service was not
granted answers 403. The hub's ``_optional_namespace`` swallows that on purpose -- a
settings outage must not take a turn down -- which makes a missing grant indistinguishable
from a sibling being briefly unreachable, forever.

It happened: the generator granted ``lucy-api`` only the ``lucy`` namespace, while the hub
resolves ``search`` and ``spotify`` on every turn for the person's search backend, result
count and playback device. Every turn ever served logged two 403s nobody read, and those
preferences were ignored while the settings page went on showing them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from lucy_api.core.container import (
    MUSIC_NAMESPACE,
    NAMESPACES_READ,
    OWN_NAMESPACE,
    SEARCH_NAMESPACE,
)

META_ROOT = Path(__file__).resolve().parents[2]
HUB = "lucy-api"


def grants() -> dict[str, tuple[str, ...]]:
    """`SETTINGS_GRANTS` read out of the generator, without importing it.

    Read rather than imported for the same reason the audience check reads it: the
    generator writes a file, and importing it to ask what it would write is a longer way
    round with more that can go wrong.
    """
    source = (META_ROOT / "scripts" / "genenv.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        targets = getattr(node, "targets", []) or ([node.target] if hasattr(node, "target") else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "SETTINGS_GRANTS":
                rows = ast.literal_eval(node.value)
                return {name: tuple(namespaces) for name, _, namespaces, _ in rows}
    msg = "SETTINGS_GRANTS is not assigned at the top level of scripts/genenv.py"
    raise AssertionError(msg)


@pytest.mark.parametrize("namespace", NAMESPACES_READ)
def test_every_namespace_the_hub_reads_is_one_it_was_granted(namespace: str) -> None:
    assert namespace in grants()[HUB]


def test_the_hub_reads_more_than_its_own_namespace() -> None:
    """The bug itself, named. Granting only `lucy` looks right until you notice the hub
    folds two siblings' defaults into every turn."""
    assert NAMESPACES_READ != (OWN_NAMESPACE,)
    assert {SEARCH_NAMESPACE, MUSIC_NAMESPACE} <= set(NAMESPACES_READ)


def test_the_hub_is_not_granted_namespaces_it_never_reads() -> None:
    """A grant is authority. One nothing asks for is authority nobody meant to hand over."""
    assert set(grants()[HUB]) == set(NAMESPACES_READ)


@pytest.mark.parametrize("namespace", [SEARCH_NAMESPACE, MUSIC_NAMESPACE])
def test_a_sibling_still_owns_the_namespace_the_hub_borrows(namespace: str) -> None:
    """Reading a sibling's namespace does not take it from the sibling: the service whose
    settings they are must still be granted them, or it cannot write its own defaults."""
    owners = [name for name, namespaces in grants().items() if namespace in namespaces]
    assert HUB in owners
    assert len(owners) > 1
