"""A model you program, because a loop you cannot program is a loop you cannot test.

The agent loop is the hardest thing in this system to get right and the easiest to
smoke-test into a false sense of security. Asserting on it needs a model that emits
exactly this plan, fails exactly there, and returns something malformed on the third
call -- every time, offline, in milliseconds. That is what this is.

## Every outcome the loop has to survive is a factory function

`speaks`, `plans`, `malformed`, `runs_out_of_room`, `refuses`, `flakes`, `fails`. A test
reads as the transcript it is asserting, and the awkward paths are as cheap to write as
the happy one, which is the only way they get written. The seam's two exceptions and the
wire's third are all reachable, so a test can prove that a transient failure is retried
and a permanent one is not without knowing which vendor raised it.

## It records every request

`requests` is the whole point of the fake beside its script. The interesting assertion is
rarely "what did the model say" -- the test wrote that -- it is "what was the model
*asked*": which sections the assembler included, in what order, with the state block last
and the tool results framed. Keeping every `Request` makes that a plain equality check.

## Running off the end is loud

A script that repeats its last reply when exhausted turns a loop that fails to terminate
into a suite that hangs, or worse, one that passes. `ScriptExhaustedError` names how many
replies were asked for and how many were written, because that number is usually the bug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lucy_api.model.types import (
    Chunk,
    ModelRefusedError,
    ModelUnavailableError,
    Reply,
    Stop,
    Usage,
)
from lucy_api.model.wire import CHUNK_DONE, CHUNK_REASONING, CHUNK_TEXT, ModelCallFailedError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from lucy_api.model.types import Provider, Request

NO_USAGE = Usage()
"""The default cost of a scripted reply. A test that asserts accounting passes its own."""

MALFORMED_PLAN: dict[str, Any] = {"steps": "look it up and tell me"}
"""Valid JSON, invalid plan: `steps` is a sentence where every schema wants a list."""

DEFAULT_MODEL = "scripted"


@dataclass(frozen=True, slots=True)
class Step:
    """One scripted outcome: a reply to hand back, or an exception to raise instead.

    Built through the factories below rather than directly, so there is no such thing as
    a step that is both and no such thing as a step that is neither.
    """

    reply: Reply = field(default_factory=Reply)
    error: Exception | None = None

    def serve(self) -> Reply:
        """Do whatever this step says happens on this call."""
        if self.error is not None:
            raise self.error
        return self.reply


class ScriptExhaustedError(Exception):
    """The loop asked for a reply the script does not have."""


def speaks(
    text: str,
    *,
    reasoning: str = "",
    usage: Usage = NO_USAGE,
    model: str = DEFAULT_MODEL,
    stop: Stop = Stop.end_turn,
) -> Step:
    """It said something and stopped. Also how you script output that is not valid JSON."""
    return Step(reply=Reply(text=text, reasoning=reasoning, stop=stop, usage=usage, model=model))


def plans(
    plan: dict[str, Any],
    *,
    text: str = "",
    reasoning: str = "",
    usage: Usage = NO_USAGE,
    model: str = DEFAULT_MODEL,
) -> Step:
    """It answered with steps to run. `Stop.tool_use`, because the turn is not over."""
    return Step(
        reply=Reply(
            text=text,
            plan=plan,
            reasoning=reasoning,
            stop=Stop.tool_use,
            usage=usage,
            model=model,
        )
    )


def malformed(plan: dict[str, Any] | None = None, *, text: str = "") -> Step:
    """It answered with a plan no schema will accept, so the repair path can be tested.

    This is the common half of malformed: well-formed JSON in the wrong shape. The other
    half -- output that is not JSON at all -- is `speaks("{\\"steps\\": [")`, because that
    is exactly what a real provider hands back when the model's JSON does not parse.
    """
    return Step(
        reply=Reply(
            text=text,
            plan=dict(MALFORMED_PLAN) if plan is None else plan,
            stop=Stop.tool_use,
            model=DEFAULT_MODEL,
        )
    )


def runs_out_of_room(text: str = "", *, usage: Usage = NO_USAGE) -> Step:
    """It hit `max_output_tokens` mid-sentence. Resumable, and a client may offer that."""
    return Step(reply=Reply(text=text, stop=Stop.max_tokens, usage=usage, model=DEFAULT_MODEL))


def refuses(reason: str) -> Step:
    """It declined, the way a real provider declines: by raising rather than replying."""
    return Step(error=ModelRefusedError(reason))


def flakes(reason: str, *, retry_after: float | None = None) -> Step:
    """It was rate-limited or overloaded. The loop is expected to try this call again."""
    return Step(error=ModelUnavailableError(reason, retry_after=retry_after))


def fails(reason: str) -> Step:
    """A revoked key, a retired model, a schema the provider refuses. Never retried."""
    return Step(error=ModelCallFailedError(reason))


def raises(error: Exception) -> Step:
    """Whatever else you need to happen. The general case the others are sugar for."""
    return Step(error=error)


class ScriptedProvider:
    """A model that says what you told it to say, in order, once each."""

    name: str = "scripted"

    def __init__(
        self,
        script: Sequence[Step] = (),
        *,
        model: str = DEFAULT_MODEL,
        chunk_size: int = 8,
    ) -> None:
        if chunk_size < 1:
            msg = f"chunk_size must be at least 1 character, not {chunk_size}"
            raise ValueError(msg)
        self._script = tuple(script)
        self._chunk_size = chunk_size
        self.model = model
        self.requests: list[Request] = []
        """Every request this provider was handed, in order, including refused ones."""

    @property
    def calls(self) -> int:
        """How many replies have been asked for."""
        return len(self.requests)

    @property
    def remaining(self) -> int:
        """How many scripted steps are still unused. Zero at the end of a good test."""
        return max(0, len(self._script) - len(self.requests))

    async def complete(self, request: Request) -> Reply:
        """Serve the next step, or say loudly that there is not one."""
        return self._next(request).serve()

    async def stream(self, request: Request) -> AsyncIterator[Chunk]:
        """The same step, delivered in pieces, so a consumer's reassembly is exercised."""
        reply = self._next(request).serve()
        if reply.reasoning:
            yield Chunk(kind=CHUNK_REASONING, text=reply.reasoning)
        for start in range(0, len(reply.text), self._chunk_size):
            yield Chunk(kind=CHUNK_TEXT, text=reply.text[start : start + self._chunk_size])
        yield Chunk(kind=CHUNK_DONE, reply=reply)

    def _next(self, request: Request) -> Step:
        """Record the request first: what the loop asked for on the call that ran off the
        end of the script is the most useful thing in the failure."""
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self._script):
            msg = (
                f"the scripted model was asked for reply {index + 1} but the script has "
                f"{len(self._script)}. Add a step for what the loop does next, or assert "
                "that it stops sooner. Repeating the last reply would let a loop that "
                "never terminates pass this test."
            )
            raise ScriptExhaustedError(msg)
        return self._script[index]


if TYPE_CHECKING:

    def _the_fake_satisfies_the_seam(fake: ScriptedProvider) -> Provider:
        """mypy is what enforces this, and nothing else can: a fake that drifts from the
        Protocol is a fake that proves the wrong thing."""
        return fake


__all__ = [
    "DEFAULT_MODEL",
    "MALFORMED_PLAN",
    "NO_USAGE",
    "ScriptExhaustedError",
    "ScriptedProvider",
    "Step",
    "fails",
    "flakes",
    "malformed",
    "plans",
    "raises",
    "refuses",
    "runs_out_of_room",
    "speaks",
]
