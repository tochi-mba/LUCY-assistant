"""How a person wants Lucy to work with them: when to ask, when to offer a view, whether to
narrate, and whether to mention what it kept.

Four lucy settings, each a matter of temperament rather than something one prompt can decide
for everybody. Some people hate a round trip and others hate a wrong guess; some want a
sparring partner and others want the thing done without commentary. The authored sections
already say what Lucy does when nobody chose, and those are the defaults here:

- `ambiguity` `assume_and_say`: take the careful reading and say which in one line
  (`behaviour`).
- `opinions` `when_they_matter`: give a view when it matters, once (`identity`).
- `progress_updates` `before_long_steps`: say what is happening before a long step
  (`behaviour`).
- `announce_memory_writes` on: say in a clause that something was kept (`memory`).

A default says nothing, so a person who never opened settings is shown the prompt they have
always been shown, to the byte.

A choice is stated in the `preferences` section, beside the person's conventions, and not
appended to the section whose rule it changes. That section is the one a person's choices
live in for two reasons that apply here as well: `behaviour` runs close to its ceiling, so a
line added at its end is the first one cut, and a person may leave `behaviour` or `memory`
out of the prompt altogether, which must not quietly undo something else they chose. Because
the rule a choice changes is written elsewhere -- sometimes above `preferences` and sometimes
below it -- the sentence says that the person's choice wins where the two differ.

None of this is a floor. Whether a destructive or outward action asks first is the permission
gate's decision, whatever these say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

AMBIGUITY = frozenset({"assume_and_say", "ask_first"})
OPINIONS = frozenset({"when_they_matter", "only_when_asked"})
PROGRESS = frozenset({"before_long_steps", "quiet"})

PREAMBLE = (
    "This person chose how you work with them, and where that differs from the rest of this "
    "prompt, their choice wins."
)


def _one_of(value: object, allowed: frozenset[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


@dataclass(frozen=True, slots=True)
class Manner:
    """One person's choices, already checked. The defaults are the prompt as authored."""

    ambiguity: str = "assume_and_say"
    opinions: str = "when_they_matter"
    progress_updates: str = "before_long_steps"
    announce_memory_writes: bool = True

    @classmethod
    def from_reader(cls, read: Callable[[str, Any], Any]) -> Manner:
        """Build from whatever reads one resolved key with a fallback."""
        return cls(
            ambiguity=_one_of(read("ambiguity", "assume_and_say"), AMBIGUITY, "assume_and_say"),
            opinions=_one_of(read("opinions", "when_they_matter"), OPINIONS, "when_they_matter"),
            progress_updates=_one_of(
                read("progress_updates", "before_long_steps"), PROGRESS, "before_long_steps"
            ),
            announce_memory_writes=read("announce_memory_writes", True) is not False,
        )

    def hint(self) -> str:
        """What the prompt says about working with this person. Empty when nothing was chosen."""
        chosen: list[str] = []
        if self.ambiguity == "ask_first":
            chosen.append(
                "When a request could reasonably mean two things, ask which one they meant "
                "before acting, in one question."
            )
        if self.opinions == "only_when_asked":
            chosen.append(
                "Give an opinion only when they ask for one; otherwise do what was asked."
            )
        if self.progress_updates == "quiet":
            chosen.append("Do not narrate what you are about to do; report what happened.")
        if not self.announce_memory_writes:
            chosen.append(
                "When you keep something about them, do not say so unless they ask; it is "
                "still theirs to read, correct and delete."
            )
        if not chosen:
            return ""
        return f"{PREAMBLE} {' '.join(chosen)}"


AS_AUTHORED = Manner()
"""What a person who has set none of them has. Frozen, so one value serves everyone."""

__all__ = ["AMBIGUITY", "AS_AUTHORED", "OPINIONS", "PREAMBLE", "PROGRESS", "Manner"]
