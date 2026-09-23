"""`Decisions`: the one place a judgement is asked for, and the only one that can raise.

It cannot, in fact, raise. :meth:`Decisions.ask` swallows everything a decider can do wrong --
a timeout, a malformed payload, an exception from a third-party client -- and answers with
empty :class:`~weftai.decisions.Answers`, which every reader in this package treats as "decide
exactly as the code did before". That is the whole contract, and `tests/hub` pins it by
running a turn with a decider and without one and comparing the items byte for byte.

This package is a leaf. It imports `weftai` and `lucy_api.settings.policy` and nothing else of
Lucy's; the event emitter arrives as an injected callable, so the import-linter contracts hold
without amendment and a test can watch what was emitted without a container.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from weftai.decisions import Answers, AnyQuestion, Decider, NullDecider

from lucy_api.decide.types import TOPIC, USES, Direction, Skip, Use

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
"""How a decision reaches the stream: an event name and its fields, never the state text."""

MADE = "lucy.decision.made"
SKIPPED = "lucy.decision.skipped"
DISAGREED = "lucy.decision.disagreed"
"""The decision and the deterministic path reached different answers. In shadow mode this is
the only thing a person has to go on, so it is the event worth watching."""


async def _silent(name: str, fields: dict[str, Any]) -> None:
    _ = name, fields


class Decisions:
    """Held on the container and on `PackContext.decide`, exactly as `work` is.

    `enabled` is the set of use ids whose own setting is on; `shadow` is the set whose answers
    must be discarded after being measured. Both are computed once at turn start from
    `TurnPolicy` and held for the life of the turn, like every other knob a turn reads.
    """

    def __init__(  # noqa: PLR0913 - the three gates, the budget and the emitter are independent
        self,
        decider: Decider | None = None,
        *,
        enabled: Sequence[str] = (),
        shadow: bool = True,
        timeout_ms: int = 1000,
        max_per_turn: int = 8,
        emit: Emit | None = None,
    ) -> None:
        self.decider = decider or NullDecider()
        self.enabled = frozenset(enabled)
        self.shadow = shadow
        self.timeout_ms = timeout_ms
        self.max_per_turn = max_per_turn
        self._emit = emit or _silent
        self._spent = 0

    @property
    def spent(self) -> int:
        """Calls made this turn, against `lucy.decision_max_per_turn`."""
        return self._spent

    def live(self, use: Use) -> bool:
        """Whether this use's answer may be acted on: enabled, and not in shadow."""
        return use.id in self.enabled and not self.shadow

    async def ask(self, use: Use, state: str, questions: Sequence[AnyQuestion]) -> Answers:
        """Ask, and answer with nothing rather than raising, whatever happens.

        Returns empty answers when the use is off, the budget is spent, there is no decider,
        the call times out, or the decider misbehaves. A caller never has to distinguish
        those: every one of them means the deterministic path decides.
        """
        refusal = self._why_not(use)
        if refusal is not None:
            await self._skip(use, refusal)
            return Answers()

        self._spent += 1
        started = time.monotonic()
        try:
            answers = await asyncio.wait_for(
                self.decider.decide(state, questions), self.timeout_ms / 1000
            )
        except TimeoutError:
            await self._skip(use, Skip.TIMEOUT)
            return Answers()
        except Exception:
            # A third-party client may raise anything at all. The type name would be useful
            # and the message would not: a client often puts the response body in it, and a
            # response body has no business in an event.
            await self._skip(use, Skip.MALFORMED)
            return Answers()

        latency_ms = int((time.monotonic() - started) * 1000)
        if not isinstance(answers, Answers) or answers.empty:
            await self._skip(use, Skip.MALFORMED, latency_ms=latency_ms)
            return Answers()

        await self._emit(
            MADE,
            {
                "use": use.id,
                "shadow": self.shadow,
                "direction": use.direction,
                "latency_ms": latency_ms,
                "answers": [
                    {"id": a.id, "kind": a.kind, "value": a.value, "probability": a.probability}
                    for a in answers
                ],
            },
        )
        return answers

    def _why_not(self, use: Use) -> Skip | None:
        """Why this use will not be asked, or `None` when it will.

        Gathered here rather than spread through :meth:`ask` so that the reasons read as one
        list, which is also how the docs page and the event vocabulary present them.
        """
        if use.id not in self.enabled:
            return Skip.OFF
        if isinstance(self.decider, NullDecider):
            return Skip.NO_DECIDER
        if self.max_per_turn and self._spent >= self.max_per_turn:
            return Skip.TURN_BUDGET
        return None

    async def disagreed(self, use: Use, *, decided: str, fallback: str) -> None:
        """Record that the decision and the deterministic path differ.

        In shadow mode this is the only signal a person has, and it is what turns "try it and
        see" into a number. Both values are Lucy's own -- a topic key, a verdict name -- and
        never anything the decider was shown.
        """
        if decided == fallback:
            return
        await self._emit(
            DISAGREED,
            {"use": use.id, "shadow": self.shadow, "decided": decided, "fallback": fallback},
        )

    async def _skip(self, use: Use, reason: Skip, *, latency_ms: int | None = None) -> None:
        fields: dict[str, Any] = {"use": use.id, "reason": str(reason)}
        if latency_ms is not None:
            fields["latency_ms"] = latency_ms
        await self._emit(SKIPPED, fields)


__all__ = [
    "DISAGREED",
    "MADE",
    "SKIPPED",
    "TOPIC",
    "USES",
    "Decisions",
    "Direction",
    "Emit",
    "Skip",
    "Use",
]
