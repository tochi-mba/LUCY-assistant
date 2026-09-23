"""Every word the model reads about a capability lives in one place, and is held to one bar.

The pages in `prompt/capabilities/` are prompt. They are tested like the stable sections:
one page per installed capability and no orphans, a heading a person would recognise, a
ceiling on length, and none of the words that would tell the model about the wire.
"""

from __future__ import annotations

import re
from pathlib import Path

from lucy_api.packs.mcp import McpPack
from lucy_api.packs.service import installed_packs
from lucy_api.packs.watch import WatchPack
from lucy_api.prompt.docs import FOLDER, capability_doc, capability_docs, read_capability_doc

MAX_PAGE_CHARS = 2_000
"""A page is loaded on demand into a windowed read; one that needs more than this is a manual."""

LEAKS = (
    r"[a-z]+-api",
    r"spotify",
    r"keyring",
    r"weftai",
    r"uvicorn",
    r"fastapi",
    r"sqlite",
    r"localhost",
    r"127\.0\.0\.1",
    r"\bport\b",
    r"\b80\d\d\b",
)
"""What a page must not say. `url` is deliberately absent: a public address the person
hands over is the model's business, and one operation names its field that way."""

VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def packs() -> dict[str, object]:
    every = (*installed_packs(), WatchPack("https://workspace.test"), McpPack.__new__(McpPack))
    return {str(pack.id): pack for pack in every}


def test_every_capability_with_docs_has_exactly_its_own_page_and_there_are_no_orphans() -> None:
    with_pages = {
        pack_id for pack_id, pack in packs().items() if pack_id != "mcp" and pack.docs is not None
    }

    assert with_pages == set(capability_docs())
    for pack_id in with_pages:
        page = capability_doc(pack_id)
        assert isinstance(page, Path)
        assert page.name == f"{pack_id}.md"
        assert page.parent.name == FOLDER
        assert packs()[pack_id].docs == page


def test_every_page_opens_with_one_heading_fits_the_window_and_names_no_wire() -> None:
    for pack_id in capability_docs():
        text = read_capability_doc(pack_id)
        assert text.startswith("# "), f"{pack_id}.md does not open with a heading"
        assert text.count("\n# ") == 0, f"{pack_id}.md has a second top-level heading"
        assert len(text) <= MAX_PAGE_CHARS, f"{pack_id}.md is {len(text)} characters"
        assert text.endswith("\n")
        for word in LEAKS:
            assert not re.search(word, text, re.IGNORECASE), f"{pack_id}.md names {word}"
        for verb in VERBS:
            assert not re.search(rf"\b{verb}\b", text), f"{pack_id}.md names {verb}"


def test_external_tools_deliberately_have_no_page() -> None:
    """Their docs come from the servers the person registered, and are untrusted."""
    assert McpPack.__new__(McpPack).docs is None
