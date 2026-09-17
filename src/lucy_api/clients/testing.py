"""A sibling that is not there, for tests that are about everything except the network.

Two things, for two jobs.

`FakeHttp` is the seam swapped out: it answers from a script and remembers every `Call` it
was handed, so a test can assert on the *request* -- which audience the token was minted
for, which profile header travelled, what the body said -- as well as on what came back.
That matters more here than in most clients, because a call to the right URL with the wrong
audience is a token somebody else can replay and is invisible in the response.

`Answer` and `problem` are the canned responses. `problem` builds an RFC 9457 document
because that is what every service in this family sends when it says no, and a test that
hand-rolls one tends to hand-roll the field the client actually reads.

This lives in `src` rather than in the suite for the reason `settings_client.testing` does:
the packs are tested against these fakes too, and a fake that only the client's own tests
can reach is a fake that drifts from the client it stands in for.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lucy_api.packs.context import Call

PROBLEM_BASE = "https://service.invalid/problems"


@dataclass(frozen=True, slots=True)
class Answer:
    """One canned response, in the shape `lucy_api.clients.errors.Response` describes."""

    status_code: int = 200
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """The decoded body, which a scripted answer already has decoded."""
        return self.body


def problem(status: int, *, code: str = "", detail: str = "", **headers: str) -> Answer:
    """A failure in the family's one error shape, with the discriminator a client reads.

    `code` becomes the last segment of `type`, which is where every service in the family
    puts the stable word -- `credential-unavailable`, `no-active-device` -- and is the thing
    a client switches on rather than the prose.
    """
    body: dict[str, Any] = {"status": status, "title": "Problem", "detail": detail}
    if code:
        body["type"] = f"{PROBLEM_BASE}/{code}"
    return Answer(status_code=status, body=body, headers=headers)


class ExhaustedError(LookupError):
    """A client made a call the test did not script.

    Louder than a default answer on purpose: a test that gets an empty 200 it did not write
    passes for the wrong reason, and the call it did not expect is usually the bug.
    """

    def __init__(self, call: Call) -> None:
        super().__init__(f"no answer scripted for {call.method} {call.url}")
        self.call = call


class FakeHttp:
    """An `Http` that answers from a script and records what it was asked.

    Answers are handed out in order. Recording the `Call` rather than a URL string is
    deliberate: `audience` is the field most worth asserting on and the one a string would
    lose.
    """

    def __init__(self, *answers: Answer) -> None:
        self.answers = deque(answers)
        self.calls: list[Call] = []

    async def request(self, call: Call) -> Answer:
        """Record the call and hand back the next scripted answer.

        Raises:
            ExhaustedError: the script ran out, which means the client called something the
                test did not expect.
        """
        self.calls.append(call)
        if not self.answers:
            raise ExhaustedError(call)
        return self.answers.popleft()

    @property
    def last(self) -> Call:
        """The most recent call, which in most tests is the only one.

        Raises:
            LookupError: nothing was called at all. An assertion about a request that never
                happened must fail loudly rather than read an empty list.
        """
        if not self.calls:
            message = "nothing was called"
            raise LookupError(message)
        return self.calls[-1]

    async def request_response(self, call: Call) -> Answer:
        """Return the scripted response, including its status and headers."""
        return await self.request(call)


__all__ = ["PROBLEM_BASE", "Answer", "ExhaustedError", "FakeHttp", "problem"]
