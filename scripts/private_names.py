"""The names a public repository must never use, and how to recognise them however written.

ADR-0011: a public repository never names a private service. `parity.py`'s private-names
check asks this module two things -- which repositories are private on this machine, and what
each of them looks like in text -- and searches the public repositories for the answers.

Standard library only, like `parity.py`, which imports it.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ALSO_KNOWN_AS = ("tool", "lucy", "also-known-as")
"""Where a private repository's own pyproject.toml lists the other names it goes by."""


class DeclarationError(ValueError):
    """A private repository's list of other names is not a list of names."""


def manifest_names(root: Path) -> tuple[str, ...]:
    """The repositories this machine has that the family does not publish.

    Read from the gitignored local manifest, never from a list in a public file. A denylist
    that names the thing it is hiding has already published it, which is the whole reason
    ADR-0011 exists.
    """
    manifest = root / ".repos.local.txt"
    if not manifest.is_file():
        manifest = root / "repos.local.txt"
    if not manifest.is_file():
        return ()
    names = []
    for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = line.split("#", 1)[0].split()
        if entry:
            names.append(entry[0])
    return tuple(names)


def also_known_as(checkout: Path, name: str) -> tuple[str, ...]:
    """The other spellings a private repository says it goes by, read from its checkout.

    A repository's name is rarely the only way anybody writes it: a service called
    ``Example-api`` turns up as ``example:`` in a URI, or as its product's name in prose.
    Only the private repository knows those, and only it may say them, so it lists them
    under ``[tool.lucy] also-known-as`` in its own pyproject.toml. A public file listing
    them would be the leak the list exists to catch. A checkout that is not here, or that
    declares nothing, adds nothing.
    """
    pyproject = checkout / "pyproject.toml"
    if not pyproject.is_file():
        return ()
    wrong = f"{name}'s pyproject.toml: tool.lucy.also-known-as must be a list of names"
    try:
        data: Any = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        message = f"{name}'s pyproject.toml is not valid TOML: {exc}"
        raise DeclarationError(message) from None
    for key in ALSO_KNOWN_AS:
        data = data.get(key) if isinstance(data, dict) else None
    if data is None:
        return ()
    if not isinstance(data, list) or not all(isinstance(item, str) and item for item in data):
        raise DeclarationError(wrong)
    return tuple(data)


def spelling(text: str) -> re.Pattern[str]:
    """One name, however it is joined and cased, and only as a whole word.

    ``Example-Tool``, ``example_tool``, ``Example Tool``, ``EXAMPLETOOL``: a leak is a leak
    whichever separator somebody reached for, and each is the spelling a grep for another
    would miss. A letter or digit either side means a different word, so a name that happens
    to sit inside a longer one is left alone.
    """
    parts = [re.escape(part) for part in re.split(r"[-_ ]+", text.strip()) if part]
    body = "[-_ ]?".join(parts)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def patterns(
    names: tuple[str, ...], locate: Callable[[str], Path]
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Each private name and every spelling its repository declares, labelled by the name.

    ``locate`` says where a repository's checkout would be; a failure is reported under the
    repository's name only, never the spelling that matched.
    """
    return tuple(
        (name, spelling(written))
        for name in names
        for written in (name, *also_known_as(locate(name), name))
    )
