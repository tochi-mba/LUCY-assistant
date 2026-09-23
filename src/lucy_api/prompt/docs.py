"""Where a capability's authored markdown lives, and how a pack finds its own.

Every word the model reads is prompt, and prompt is authored text with a home: the stable
sections in `defaults/`, and one page per capability in `capabilities/`. A pack does not
carry its page as a string constant beside its handlers, because a prompt that lives in
code is edited like code -- in a diff nobody reads for tone -- and reviewed by nobody who
reviews prompts. Here the pages sit together, are read through `importlib.resources` so the
wheel and the checkout agree, and are held to the same tests as the sections: no service
names, no ports, no verbs off the wire, a ceiling on length.

A pack asks for its page by its own id. A pack whose page is missing has no docs, which
`help.docs` reports as such rather than inventing a summary.
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

PACKAGE = "lucy_api.prompt"
FOLDER = "capabilities"


def capability_doc(capability_id: str) -> Path:
    """The page for one capability, by id. A path, so `help.docs` reads it on demand."""
    return files(PACKAGE) / FOLDER / f"{capability_id}.md"  # type: ignore[return-value]


def read_capability_doc(capability_id: str) -> str:
    """The page's text, for the one caller that needs it in hand rather than by reference."""
    return capability_doc(capability_id).read_text(encoding="utf-8")


def capability_docs() -> tuple[str, ...]:
    """Every capability that has a page, by id. What the tests check against the packs."""
    folder = files(PACKAGE) / FOLDER
    return tuple(
        sorted(
            item.name.removesuffix(".md") for item in folder.iterdir() if item.name.endswith(".md")
        )
    )


__all__ = ["FOLDER", "PACKAGE", "capability_doc", "capability_docs", "read_capability_doc"]
