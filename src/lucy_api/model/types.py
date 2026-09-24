"""The seam between Lucy and whoever is doing the thinking.

One Protocol, three implementations: a real provider, a scripted fake, and whatever comes
next. The loop is written against this and never against a vendor's SDK, for three reasons
that are all the same reason.

**A turn must be testable without a network.** The agent loop is the hardest thing in this
system to get right and the easiest to smoke-test into a false sense of security. Golden
transcripts -- a scripted model that emits exactly these plans, fails exactly there,
returns malformed output on the third call -- are the only way to assert the *exact* item
log, the *exact* event stream and the *exact* token accounting. That needs a model you can
program.

**Providers disagree about everything except the shape below.** What a "message" is, how
tool calls arrive, whether thinking is a block or a field, what a stop reason is called.
Pushing that into one adapter per provider keeps the disagreement in one file instead of
threaded through the loop.

**The stop reason is load-bearing and providers name it badly.** `max_tokens` is resumable
and `refusal` is not; a client has to show them differently, and a loop has to decide
differently. `Stop` below is Lucy's vocabulary, and each adapter maps into it.

## What this deliberately does not model

Streaming deltas are a transport concern and live in `lucy_api.stream`. This is the shape
of one request and one reply, plus an async iterator of chunks for the streaming case. The
loop can run entirely on the non-streaming path, which is what the golden transcripts use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence


class Role(StrEnum):
    """Who said a thing. `system` is the assembled prompt, not a conversational turn."""

    system = "system"
    user = "user"
    assistant = "assistant"


class Stop(StrEnum):
    """Why the model stopped, in Lucy's words rather than a vendor's.

    Kept separate from `turns.termination`, which is why *Lucy* stopped. A turn can end in
    `error_max_iterations` while every model call in it ended in `end_turn`, and a client
    that conflates the two will offer to resume something that cannot be resumed.
    """

    end_turn = "end_turn"
    """It finished saying what it had to say."""

    max_tokens = "max_tokens"
    """It ran out of room. Resumable: ask again with more."""

    tool_use = "tool_use"
    """It wants something run before it can continue."""

    refusal = "refusal"
    """It declined. Not resumable, and a client must not offer to retry it."""

    cancelled = "cancelled"
    """Somebody stopped it."""


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of the conversation as the provider will see it."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Usage:
    """What a call cost, including what the cache saved.

    `cache_read` is reported separately rather than folded into `input`, because the whole
    argument for ordering the prompt by volatility is that this number stays large. A
    system that cannot see it cannot tell when a change quietly ended the prefix.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micros: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_micros=self.cost_micros + other.cost_micros,
        )


@dataclass(frozen=True, slots=True)
class Reply:
    """One model reply.

    `plan` is what makes this weftai rather than a chat loop: the model answers with steps
    to run, not with a tool call to copy values out of. It is `None` when the model simply
    spoke, which is the common case and must stay cheap.
    """

    text: str = ""
    plan: dict[str, Any] | None = None
    reasoning: str = ""
    stop: Stop = Stop.end_turn
    usage: Usage = field(default_factory=Usage)
    model: str = ""


@dataclass(frozen=True, slots=True)
class Chunk:
    """A piece of a reply as it arrives. `kind` is what the stream encoder switches on."""

    kind: str
    text: str = ""
    reply: Reply | None = None


SAY = "say"
"""The plan's one field for words: what the person is told, with steps or instead of them.

The plan schema is sent as structured output, which a provider that can enforce it does
enforce, and it used to require `steps`. A model asked "hi" through such a provider had no
way to answer: it was made to invent a step -- through clyde, `capabilities.list` in reply to
a greeting, twice in two -- and a turn could end only on its iteration cap. Anthropic's and
OpenAI's own adapters send the same schema the same way.
"""

SAY_DESCRIPTION = (
    "What to tell the person. On its own, with no steps, it is your answer and ends the turn. "
    "Beside steps, it is said before they run -- what you are about to do, never that it is "
    "done, since a step can fail or wait for the person's approval."
)
"""How `say` describes itself inside the schema, which is all some models ever read of it."""


@dataclass(frozen=True, slots=True)
class Request:
    """One call. The prompt is already assembled; this layer does not build context.

    `plan_schema` is handed over rather than derived, because which operations a person can
    see is decided by the capability layer and must not be rediscoverable from here.
    """

    messages: Sequence[Message]
    system: str = ""
    plan_schema: dict[str, Any] | None = None
    max_output_tokens: int = 4096
    temperature: float | None = None
    thinking: str = "default"
    model: str = ""
    max_thinking_tokens: int = 0


class ModelUnavailableError(Exception):
    """The provider could not be reached, or refused for a reason worth retrying."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ModelRefusedError(Exception):
    """The provider declined. Never retried, and surfaced to the person as a refusal."""


class Provider(Protocol):
    """Whoever does the thinking."""

    name: str

    async def complete(self, request: Request) -> Reply: ...

    def stream(self, request: Request) -> AsyncIterator[Chunk]: ...


__all__ = [
    "SAY",
    "SAY_DESCRIPTION",
    "Chunk",
    "Message",
    "ModelRefusedError",
    "ModelUnavailableError",
    "Provider",
    "Reply",
    "Request",
    "Role",
    "Stop",
    "Usage",
]
