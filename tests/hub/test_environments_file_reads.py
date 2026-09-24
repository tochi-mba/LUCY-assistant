"""A file read is a window of bytes, and the in-memory workspace serves it as the sandbox does.

Environments-api reads a byte window, ends it on a character boundary and refuses to start
one inside a character (Environments-api app/files.py:115-149). The fake the packs are
tested against used to slice a str, which can never refuse, so every behaviour that depends
on bytes passed against it and failed against the sandbox.
"""

from __future__ import annotations

import pytest

from lucy_api.clients.environments import MID_CHARACTER, Environment, FakeEnvironmentsClient
from lucy_api.clients.errors import RejectedError

ENV = "env-1"


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
