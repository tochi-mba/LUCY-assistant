"""The boundary every tool result and every child result crosses before a parent reads it.

A model has one input channel. Whatever a capability fetched, a sibling returned or a child
agent wrote arrives in the same token stream as the person's own words, and the model has no
out-of-band way to tell the two apart. So text that imitates the harness -- a closing control
tag, a ``Human:`` turn marker, a forged ``[harness: ...]`` line -- is an attempt to borrow the
authority of the frame it is sitting inside. Taking that borrowed authority away is the whole
job of this module.

## Modify, never delete

Every match is escaped in place; nothing is ever removed. Deleting a match would be wrong
twice over. It hides the attack from the person who most needs to see it, and it silently
corrupts the legitimate output that merely looks like one -- a page *about* prompt injection,
a diff of this very file, a transcript quoted in a bug report. A reader who sees ``&lt;/notes>``
knows exactly what was there and that we touched it. The escapes are HTML entities throughout,
one idiom rather than four, and every one of them is mechanically reversible.

The same rule explains why only the harness's own tag names are escaped rather than every
``<``. A research capability returns real HTML, and mangling all of it to catch a tag we do
not use would cost the product something real and buy nothing: an invented tag name has no
authority to borrow. The name is what is checked, with or without a namespace in front of it
-- see :data:`NAMESPACE` -- because a prefix is exactly how the tags with the most authority
are written, and a list that knew only the bare spelling would have been a list of the
harmless half.

## What this achieves, and what it does not

It raises the cost of an injection. It does not make one impossible, and a module claiming
otherwise would be the most dangerous file in the repository. No pattern list catches an
instruction written in plain prose, and none ever will: "prefer short answers" is
grammatically identical to an attack, so a filter that refused the attack would refuse the
product. Persona-api's ADR-0001 rejected imperative-text filtering for that reason and it is
not attempted here either.

The real defence is the two things on either side of this module. A result is *framed* as
reported data rather than concatenated into the instruction block
(:mod:`lucy_api.context.framing`), and anything with a side effect needs an approval that
untrusted text cannot give. This is the third layer, the cheapest of the three, and the one
most likely to be mistaken for the strongest.

## Scrub once, at the boundary

Running this twice escapes our own marker the second time, because a second pass cannot tell
our `[harness: ...]` line from the forged one an attacker writes -- and a rule that trusted a
marker *because it looks like ours* would be the bypass, published. The escape is visible and
reversible, so a double scrub is untidy rather than dangerous; the fix is to run it once, where
the result arrives, which is the only place that knows the result arrived.

## Names, never payloads

:class:`Scrubbed` carries the scrubbed text and the names of the patterns that matched, so a
caller can emit ``security.injection_scrubbed`` and a log line that says ``turn-marker`` and
never what the turn marker was trying to say. Logging the payload moves the attack into the
log, which is then read by the tools least prepared to treat it as data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECURITY_EVENT = "security.injection_scrubbed"
"""The event a caller emits when `Scrubbed.changed` is true. Named here so nobody invents it."""

CONTROL_TAGS: tuple[str, ...] = (
    "assistant",
    "function_calls",
    "function_results",
    "harness",
    "human",
    "instructions",
    "lucy",
    "notes",
    "persona_notes",
    "result",
    "results",
    "system",
    "thinking",
    "tool_result",
    "tool_use",
)
"""Tag names the harness itself uses. Anything else is somebody's HTML and is left alone."""

NAMESPACE = r"(?:[A-Za-z_][\w.-]*[ \t]*:[ \t]*)?"
"""An optional namespace prefix in front of one of those names.

Several names above are only ever seen with a namespace on them -- a real harness writes a
prefixed `function_calls`, never a bare one -- so matching the bare spelling alone would
catch the imitation with no authority and wave through the one carrying all of it. Any
prefix counts rather than a list of the ones we know, because the borrowed thing is the
name: `<x:system>` is a system tag wearing a hat, and a prefix nobody recognises is if
anything the more suspicious of the two. The cost is an element in a real document that is
genuinely namespaced *and* genuinely called one of the names above, which pays one visible
escape."""

TURN_ROLES: tuple[str, ...] = ("human", "assistant", "system")
"""Turn markers worth escaping. `system:` is here because a forged system turn is the
strongest of the three, and it is the reason a shell result reading `System: Linux` comes back
with its colon escaped -- a visible, reversible cost we prefer to the alternative."""


@dataclass(frozen=True, slots=True)
class _Rule:
    """One thing we recognise: its log name, how it is spotted, and how it is defanged."""

    name: str
    regex: re.Pattern[str]
    replacement: str


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "control-tag",
        re.compile(rf"<(?=\s*/?\s*{NAMESPACE}(?:{'|'.join(CONTROL_TAGS)})\b)", re.IGNORECASE),
        "&lt;",
    ),
    _Rule("special-token", re.compile(r"<(?=\|)"), "&lt;"),
    _Rule(
        "turn-marker",
        re.compile(rf"\b({'|'.join(TURN_ROLES)})([ \t]*):", re.IGNORECASE),
        r"\g<1>\g<2>&#58;",
    ),
    _Rule("harness-marker", re.compile(r"\[(?=[ \t]*harness[ \t]*:)", re.IGNORECASE), "&#91;"),
    # The marker :mod:`lucy_api.context.bands` writes when it shortens a section. Escaped
    # for the same reason `[harness: ...]` is: the model has been taught to read it as the
    # harness speaking, so a fetched page carrying it can announce an invented elision, or
    # claim nothing was cut from something that was. The shape is spelled here rather than
    # imported because this module is deliberately free of dependencies; a test renders the
    # real marker and asserts this rule catches it, so the two cannot drift apart in silence.
    _Rule(
        "elision-marker",
        re.compile(r"\[(?=[ \t]*\.\.\.[ \t]*showing\b)", re.IGNORECASE),
        "&#91;",
    ),
)
"""Applied in this order, and reported in this order, so one log line is comparable to the
next. Occurrence order would make the same attack read differently depending on where in the
payload it was written, which is a distinction the reader cannot use."""


@dataclass(frozen=True, slots=True)
class Scrubbed:
    """What crossed the boundary, and what had to be neutralised on the way.

    The text is already marked; `matched` exists so the caller can say *what* happened in an
    event and a log line without ever quoting the thing that happened.
    """

    text: str
    matched: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether anything was modified. True is the signal to emit the security event."""
        return bool(self.matched)

    @property
    def log_line(self) -> str:
        """Counts and pattern names, fit for the application log. Never a byte of the payload."""
        if not self.matched:
            return "scrubbed nothing"
        noun = "pattern" if len(self.matched) == 1 else "patterns"
        return f"scrubbed {len(self.matched)} injection {noun}: {', '.join(self.matched)}"


def scrub(text: str) -> Scrubbed:
    """Neutralise harness imitation in one tool or child result, and say what was found.

    Untouched text is returned exactly as it arrived, marker and all -- a marker on every
    result would teach the model to skip the line, and it is the difference between the two
    cases that carries the meaning.
    """
    scrubbed, matched = _apply(text)
    if not matched:
        return Scrubbed(text=scrubbed)
    return Scrubbed(text=f"{_marker(matched)}\n{scrubbed}", matched=matched)


def fence(text: str) -> str:
    """The same neutralisation with no marker, for text the caller is about to delimit itself.

    :mod:`lucy_api.context.framing` puts a body inside a block of its own and adds its own
    provenance line; a `[harness: ...]` marker inside that block would be a second, redundant
    frame. The escaping is identical, which is the point of having one function do it.
    """
    return _apply(text)[0]


def _apply(text: str) -> tuple[str, tuple[str, ...]]:
    """Every rule against the whole text, collecting the names of the ones that bit."""
    matched: list[str] = []
    for rule in _RULES:
        text, count = rule.regex.subn(rule.replacement, text)
        if count:
            matched.append(rule.name)
    return text, tuple(matched)


def _marker(names: tuple[str, ...]) -> str:
    """The prepended line. It names the shape that matched and never the text that matched it."""
    return f"[harness: neutralised {', '.join(names)}]"


__all__ = [
    "CONTROL_TAGS",
    "NAMESPACE",
    "SECURITY_EVENT",
    "TURN_ROLES",
    "Scrubbed",
    "fence",
    "scrub",
]
