"""A file read is a window of bytes, and the in-memory workspace serves it as the sandbox does.

Environments-api reads a byte window, ends it on a character boundary and refuses to start
one inside a character (Environments-api app/files.py:115-149). The fake the packs are
tested against used to slice a str, which can never refuse, so every behaviour that depends
on bytes passed against it and failed against the sandbox.

A binary file is the service's verdict, not the hub's guess: it arrives base64 with
`is_binary` set, and reaches the model as a notice rather than as the encoding.

A partial read says which bytes of how many came back, and the offset to read on from,
because that offset is the one thing the model's next call needs.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.clients.environments import (
    MID_CHARACTER,
    Environment,
    FakeEnvironmentsClient,
    HttpEnvironmentsClient,
)
from lucy_api.clients.errors import RejectedError
from lucy_api.clients.testing import Answer, FakeHttp, ReadRecorder
from lucy_api.packs.service import Capabilities
from lucy_api.packs.workspace import WorkspacePack
from lucy_api.sessions.scope import SessionScope, WorkspaceScope
from lucy_api.workspace.text import BINARY_NOTICE

if TYPE_CHECKING:
    from lucy_api.packs.context import PackContext

ENV = "env-1"
URL = "http://environments.test"


def a_workspace(*files: tuple[str, str]) -> FakeEnvironmentsClient:
    fake = FakeEnvironmentsClient()
    fake.seed(Environment(ENV, "Conversation", profile="personal"), files=files)
    return fake


async def test_the_fake_refuses_a_read_that_starts_inside_a_character() -> None:
    """The bug, named: the fake sliced characters, so an offset one byte into a `✓` read
    happily here while the sandbox answered 422."""
    fake = a_workspace(("tick.log", "\N{CHECK MARK} done"))

    with pytest.raises(RejectedError) as refused:
        await fake.read(ENV, "tick.log", offset=1)
    after = await fake.read(ENV, "tick.log", offset=3)

    assert refused.value.status == 422
    assert refused.value.code == MID_CHARACTER
    assert after.content == " done"


async def test_the_fake_counts_bytes_and_ends_a_window_on_a_character_boundary() -> None:
    fake = a_workspace(("tick.log", "a\N{CHECK MARK}b"))

    read = await fake.read(ENV, "tick.log", max_bytes=2)

    assert read.content == "a"
    assert read.size == 5
    assert read.truncated is True


# --------------------------------------------------------------------------------------
# A binary file
# --------------------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n"
"""The first eight bytes of every PNG: not valid UTF-8, so binary to the sandbox."""


def a_file_content(path: str, data: bytes, *, binary: bool) -> dict[str, Any]:
    """One read as environments-api sends it: `FileContent`'s fields (Environments-api
    app/files.py:33-45) beside the environment id (app/api/routes/files.py:59), with the
    content encoded the way `FileService.read` encodes it (app/files.py:129-143)."""
    return {
        "environment_id": ENV,
        "path": path,
        "size": len(data),
        "offset": 0,
        "content": base64.b64encode(data).decode("ascii") if binary else data.decode(),
        "encoding": "base64" if binary else "utf-8",
        "truncated": False,
        "is_binary": binary,
        "etag": '"e1"',
        "next_offset": len(data),
    }


async def test_a_binary_read_is_known_by_the_verdict_the_sandbox_sends() -> None:
    """The bug, named: the hub looked for a NUL in the content, and the sandbox sends a
    binary file base64, which never has one -- so no read was ever binary. The recorder
    proves every name the client reads is one the service sends."""
    image = ReadRecorder(a_file_content("logo.png", PNG, binary=True))
    notes = ReadRecorder(a_file_content("notes.md", b"hello", binary=False))
    client = HttpEnvironmentsClient(FakeHttp(Answer(body=image), Answer(body=notes)), URL)

    binary = await client.read(ENV, "logo.png")
    text = await client.read(ENV, "notes.md")

    assert binary.binary is True
    assert text.binary is False
    assert image.absent == set()
    assert notes.absent == set()


async def test_the_fake_serves_a_body_with_a_nul_base64_as_the_sandbox_does() -> None:
    fake = a_workspace(("blob.bin", "a\0b"))

    read = await fake.read(ENV, "blob.bin")

    assert read.binary is True
    assert read.content == base64.b64encode(b"a\0b").decode("ascii")


def a_context(capabilities: Capabilities) -> PackContext:
    """Session `sess-a`, whose workspace subtree is `sessions/sess-a`."""
    return capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope(ENV, "sess-a", ready=True),
            permission_mode="auto",
        )
    )


def a_session(*answers: Answer) -> tuple[FakeHttp, Capabilities, PackContext]:
    """The workspace pack over the real client, with the probe's `/ready` answered first."""
    ready = Answer(
        body={"status": "ready", "sandbox_tier": "container", "keyring": {"status": "ok"}}
    )
    http = FakeHttp(ready, *answers)
    capabilities = Capabilities([WorkspacePack(URL, client=HttpEnvironmentsClient(http, URL))])
    return http, capabilities, a_context(capabilities)


async def test_a_binary_read_reaches_the_model_as_a_notice_not_as_base64() -> None:
    """The bug, named: `workspace.read` on an image handed the model line-numbered base64
    and a fingerprint of it, as though that were the file's text."""
    http, capabilities, context = a_session(
        Answer(body=a_file_content("sessions/sess-a/logo.png", PNG, binary=True))
    )
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "read", "op": "workspace.read", "input": {"path": "logo.png"}}]},
        context,
    )

    assert result["steps"][0]["data"] == {
        "path": "logo.png",
        "binary": True,
        "size": len(PNG),
        "notice": BINARY_NOTICE,
    }
    assert len(http.calls) == 2


async def test_a_binary_file_is_refused_an_edit_before_any_match_is_tried() -> None:
    """The bug, named: `workspace.edit` ran its fuzzy matcher over the base64 and left the
    sandbox to refuse the result. It is refused on the read, and nothing else is sent."""
    http, capabilities, context = a_session(
        Answer(body=a_file_content("sessions/sess-a/logo.png", PNG, binary=True))
    )
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "edit",
                    "op": "workspace.edit",
                    "input": {"path": "logo.png", "old_string": "iVBOR", "new_string": "x"},
                }
            ]
        },
        context,
    )

    assert result["steps"][0]["data"] == {
        "path": "logo.png",
        "replaced": False,
        "notice": BINARY_NOTICE,
    }
    assert [call.method for call in http.calls] == ["GET", "GET"]


# --------------------------------------------------------------------------------------
# Which bytes came back
# --------------------------------------------------------------------------------------

ACCENTED = "h\N{LATIN SMALL LETTER E WITH ACUTE}llo w\N{LATIN SMALL LETTER O WITH DIAERESIS}rld"
"""Eleven characters and thirteen bytes: the difference is what a character count loses."""


def a_window(offset: int, next_offset: int) -> dict[str, Any]:
    """A window of `ACCENTED` as environments-api sends it: `FileContent`'s fields
    (Environments-api app/files.py:33-45), `next_offset` being `offset + len(data)`
    (app/files.py:140)."""
    data = ACCENTED.encode()
    return {
        "environment_id": ENV,
        "path": "notes.md",
        "size": len(data),
        "offset": offset,
        "content": data[offset:next_offset].decode(),
        "encoding": "utf-8",
        "truncated": next_offset < len(data),
        "is_binary": False,
        "etag": '"e1"',
        "next_offset": next_offset,
    }


async def test_a_partial_read_names_its_bytes_and_where_to_read_on() -> None:
    """The bug, named: the notice said `showing 5 of 13 bytes` for a window of six bytes,
    because it counted decoded characters, and it never said where the window began or
    where the next one should, so the model had no offset to continue from."""
    head = ReadRecorder(a_window(0, 6))
    tail = ReadRecorder(a_window(6, 13))
    client = HttpEnvironmentsClient(FakeHttp(Answer(body=head), Answer(body=tail)), URL)

    first = await client.read(ENV, "notes.md", max_bytes=6)
    rest = await client.read(ENV, "notes.md", offset=first.next_offset)

    assert first.notice == "showing bytes 0-6 of 13; continue with offset=6"
    assert rest.notice == "showing bytes 6-13 of 13"
    assert first.content + rest.content == ACCENTED
    assert head.absent == set()


async def test_a_whole_file_carries_no_notice() -> None:
    whole = a_window(0, 13)
    client = HttpEnvironmentsClient(FakeHttp(Answer(body=whole)), URL)

    read = await client.read(ENV, "notes.md")

    assert read.notice == ""
    assert read.next_offset == 13


async def test_the_offset_the_read_tool_names_reads_on_from_where_its_window_ended() -> None:
    """What the model is told is what it can use: the notice and `next_offset` both name
    the byte the next window starts at, and reading from it loses nothing."""
    fake = a_workspace(("sessions/sess-a/notes.md", ACCENTED))
    capabilities = Capabilities([WorkspacePack(URL, client=fake)])
    context = a_context(capabilities)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "head",
                    "op": "workspace.read",
                    "input": {"path": "notes.md", "max_bytes": 7},
                },
                {
                    "id": "rest",
                    "op": "workspace.read",
                    "input": {"path": "notes.md", "offset": 7},
                },
            ]
        },
        context,
    )

    head, rest = result["steps"][0]["data"], result["steps"][1]["data"]
    assert head["next_offset"] == 7
    assert "showing bytes 0-7 of 13; continue with offset=7" in head["notice"]
    assert rest["content"] == "1\tw\N{LATIN SMALL LETTER O WITH DIAERESIS}rld"
    assert "showing bytes 7-13 of 13" in rest["notice"]
