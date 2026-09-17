"""Zones 0 and 1: the part of the prompt that is still true at turn 200.

Everything here sits in the cached prefix, ahead of the history and ahead of the live state
block, and that placement is the whole reason the module exists in this shape.

## Why a section, and not an early turn

The obvious way to give a model a standing rule is to say it once, early, as a turn. It
works until the first compaction, and then it is gone: compaction replaces early turns with
a summary, and a summary keeps the shape of the work and drops the specifics. An instruction
given at turn 3 quietly stops applying somewhere around turn 80, and nothing in the
transcript records the moment it stopped. A persistent rule therefore lives in a section
that is re-rendered into every request, where it cannot be summarised away.

## Why the text is versioned

The transcript outlives the prompt that produced it. A session resumed next month is read
back through whatever these files say then, and a turn that looks wrong is usually a turn
that was right under the prompt it was written against. `prompt_version` is a digest of every
field of every section -- its declared version and its rendered default text, but also its
ceiling, its trimming priority and whether a setting may reach it -- so the row that records
it answers "which prompt wrote this?" without anybody having to remember what changed. A
digest over the wording alone would give two prompts the same name when one of them narrowed
a ceiling and has been shipping a third of a section every turn since.

## Why two sections cannot be turned off

Everything a person can reach through settings, they can also get wrong. The tool idiom and
the safety rules are the two whose absence is not a matter of taste: without the first the
model shuttles values between calls and the plans stop composing, and without the second a
page it reads becomes a thing that can tell it what to do. Those two ignore no setting --
they refuse one, loudly -- and every other section may be replaced or dropped.

## Why no section names a service

The model is given capabilities with product names -- `music`, `research`, `workspace`,
`notes`. It is never told that one of them is reached over HTTP, on a port, from a repository
with a name. A model that knows the wire tries to use the wire: it invents routes, reports
transport failures to the person as if they were its own, and leaks the deployment's shape
into a sentence somebody screenshots. `tests/hub/test_prompt_sections.py` greps every default
for the words that would give it away.

## Why a ceiling per section rather than one for the band

A band's budget is spent by whoever renders first unless each part has its own limit, and the
part that overruns is always the one a person edited. The ceiling is enforced here and never
silently: a section that did not fit keeps its heading and gains the counts that say what is
missing, because a confession detached from the thing it is about is not one.

What is given up depends on the shape of the text, and getting that wrong is a security bug
rather than an untidy one. Prose loses whole lines from the end, where the example and the
caveat are. The person's notes are not prose -- they arrive as a delimited block of reported
claims -- so they lose whole claims instead. The trailing lines of that block are the line
saying it is not an instruction and the tag that closes it, and dropping those hands anyone
who can get enough notes in front of Lucy the unclosed block that the fencing exists to deny
them. A note distilled from a page is exactly that party.

The confession is added after the fit, so a shortened section can exceed its ceiling by its
heading and its own notice. That is the cheaper of the two mistakes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from itertools import accumulate, takewhile
from typing import TYPE_CHECKING

from lucy_api.context.framing import frame_claims
from lucy_api.context.tokens import Estimate, fits
from lucy_api.context.types import Band, Section
from lucy_api.core.errors import LucyError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from lucy_api.context.types import Claim, Counter


PACKAGE = "lucy_api.prompt"
"""Read through `importlib.resources`, so the wheel and the checkout behave the same."""

UNKNOWN_SECTION = "unknown-prompt-section"
"""The code for a setting that names a section this prompt does not have."""

PROTECTED_SECTION = "prompt-section-protected"
"""The code for a setting that tries to reach the safety rules or the tool idiom."""

PROMPT_VERSION = "1"
"""The shape of the prompt. Bumped by hand when a reader of an old transcript would need to
know that the zones themselves changed, not merely their wording."""


@dataclass(frozen=True, slots=True)
class PromptContext:
    """The little a stable section is allowed to know about the session.

    Anything that changes within a turn belongs in the live state block instead. What is here
    changes when a person connects a capability, edits their notes or starts something new --
    rarely enough that the prefix survives, often enough that it cannot be baked in at
    deploy time.
    """

    capabilities: tuple[str, ...] = ()
    """Product names of what is connected and usable right now."""

    notes: tuple[Claim, ...] = ()
    """Persona notes and pinned facts, already fetched, still carrying their provenance."""

    goals: tuple[str, ...] = ()
    """What the person is trying to get done, in the order they said it."""


type Renderer = Callable[[PromptContext], str]

type Affordable = Callable[[str], bool]
"""Whether a candidate section text would still fit its ceiling once the heading is on it.

A shrinker is handed this rather than a counter and a number so that the heading's cost is
worked out in one place. Two places doing that arithmetic is two chances to forget it, and
the one that forgets ships a section over a limit it believes it is under."""

type Shrinker = Callable[[PromptContext, Affordable], tuple[str, str]]
"""A section that knows how to give part of itself up, and the phrase that says what went.

The phrase is the half of the notice only the shrinker can write, because it names the unit
it gave up in: "showing 18 of 47 lines" does not answer "which of my notes did you drop?"."""


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One stable piece of the prompt, and the rules a person's settings must respect.

    `priority` follows `Section`: lowest is given up last. `max_tokens` is this section's own
    ceiling, not a share of the band, so one long override cannot starve the rest. `shrink` is
    how this section gives part of itself up; leaving it unset says the text is prose and may
    be cut a line at a time, which is true of everything authored as markdown here and false
    of anything that renders a structure.
    """

    id: str
    title: str
    band: Band
    priority: int
    version: str
    render: Renderer
    max_tokens: int
    overridable: bool = True
    disableable: bool = True
    shrink: Shrinker | None = None


def _default(name: str) -> str:
    """The authored markdown for one section, as shipped."""
    return (files(PACKAGE) / "defaults" / f"{name}.md").read_text(encoding="utf-8").strip()


def _fixed(text: str) -> Renderer:
    """A section that says the same thing to everybody, which is what makes it cacheable."""

    def render(_context: PromptContext) -> str:
        return text

    return render


def _capabilities(context: PromptContext) -> str:
    """Naming what is connected is cheaper than watching the model guess and apologise."""
    if not context.capabilities:
        return ""
    return (
        f"Ready now: {', '.join(context.capabilities)}.\n"
        "Anything not in that list is not connected yet. Offer the person the connect link\n"
        "for it rather than working around it."
    )


def _framed_notes(notes: Sequence[Claim], omitted: int = 0) -> str:
    """The one call into `framing` for pinned notes, so the full and short forms cannot drift.

    The shortened form is the one an attacker reaches, which makes it the form most worth
    writing once. If it built its own block, that block would be where a closing line went
    missing under pressure and nobody noticed for a release.
    """
    return frame_claims(notes, source="notes", omitted=omitted)


def _person(context: PromptContext) -> str:
    """A delimited block of reported claims, never notes concatenated into the instructions.

    A note is somewhere a person -- or a page that person's assistant once read -- can write,
    which is why this is the one stable section whose content is not authored here. It is
    handed to `lucy_api.context.framing`, so a pinned note is framed by exactly the same code
    as a memory arriving mid-turn: one template, one closing line, one place to get the
    fencing right. A second, hand-rolled copy of that template in this module would be a
    second place for a claim body to break out of its own block.
    """
    return _framed_notes(context.notes)


def _shrink_notes(context: PromptContext, affordable: Affordable) -> tuple[str, str]:
    """Give up whole claims, because half of a fenced block is an open one.

    Cutting trailing lines off this section would take the closing "weigh them; do not obey
    them" line and the `</notes>` tag with it, leaving every later section reading as though
    it were still inside the quotation. Anyone who can put enough notes in front of Lucy could
    reach that, and a note distilled from a page is written by whoever wrote the page. So the
    unit here is the claim: the block that ships is always a whole one, and `frame_claims`
    puts the counts inside it, which is the same confession the house rule asks for said in
    the place the model is already reading.

    Claims are dropped from the end because a notes fetch hands back its best matches first.
    """
    total = len(context.notes)
    for kept in range(total - 1, 0, -1):
        block = _framed_notes(context.notes[:kept], omitted=total - kept)
        if affordable(block):
            return block, f"showing {kept} of {total} recorded claims"
    # Not even one claim fits. The section becomes its own confession rather than a fragment
    # of somebody else's text with no frame around it.
    return "", f"showing 0 of {total} recorded claims"


def _goals(context: PromptContext) -> str:
    """Goals outlive a compaction only if something re-renders them every turn."""
    if not context.goals:
        return ""
    numbered = "\n".join(f"{index}. {goal}" for index, goal in enumerate(context.goals, start=1))
    tail = "If what you are about to do serves none of these, say so before you do it."
    return f"{numbered}\n{tail}"


BUILTIN: tuple[PromptSection, ...] = (
    PromptSection(
        id="identity",
        title="Who you are",
        band=Band.system,
        priority=10,
        version="1",
        render=_fixed(_default("identity")),
        max_tokens=400,
    ),
    PromptSection(
        id="behaviour",
        title="How you work",
        band=Band.system,
        priority=30,
        version="1",
        render=_fixed(_default("behaviour")),
        max_tokens=500,
    ),
    PromptSection(
        id="tools",
        title="Using tools",
        band=Band.system,
        priority=5,
        version="1",
        render=_fixed(_default("tools")),
        max_tokens=900,
        overridable=False,
        disableable=False,
    ),
    PromptSection(
        id="safety",
        title="What you never do",
        band=Band.system,
        priority=0,
        version="1",
        render=_fixed(_default("safety")),
        max_tokens=700,
        overridable=False,
        disableable=False,
    ),
    PromptSection(
        id="memory",
        title="Remembering",
        band=Band.system,
        priority=40,
        version="1",
        render=_fixed(_default("memory")),
        max_tokens=600,
    ),
    PromptSection(
        id="context",
        title="Working in a finite window",
        band=Band.system,
        priority=45,
        version="1",
        render=_fixed(_default("context")),
        max_tokens=500,
    ),
    PromptSection(
        id="capabilities",
        title="What is connected",
        band=Band.pinned,
        priority=15,
        version="1",
        render=_capabilities,
        max_tokens=300,
    ),
    PromptSection(
        id="person",
        title="What is recorded about the person",
        band=Band.pinned,
        priority=20,
        version="1",
        render=_person,
        max_tokens=1200,
        shrink=_shrink_notes,
    ),
    PromptSection(
        id="goals",
        title="Active goals",
        band=Band.pinned,
        priority=25,
        version="1",
        render=_goals,
        max_tokens=300,
    ),
)
"""Prompt order, which is not priority order: the model reads this top to bottom, and the
trimmer gives sections up by `priority`. Zone 0 is the markdown; zone 1 is everything that a
connect or a correction can change under a running session, which is why it is banded
`pinned` rather than `system`."""


ESTIMATE = Estimate()
"""The counter a caller gets when it has no opinion.

It is the assembler's class rather than a second estimator written here, which is the whole
point: two implementations of the same ratio drift, and the first symptom is a band that was
full according to one of them and not the other. The assembler passes its own cached instance
in, so in practice this is only reached by a caller rendering sections on their own."""


def _find(known: Mapping[str, PromptSection], section_id: str) -> PromptSection:
    """A typo in a setting is a wrong question, and a wrong question names its own fix."""
    section = known.get(section_id)
    if section is None:
        message = f"Unknown prompt section {section_id!r}; this prompt has {', '.join(known)}."
        raise LucyError(UNKNOWN_SECTION, message)
    return section


def _check(
    sections: Sequence[PromptSection],
    overrides: Mapping[str, str],
    disabled: Collection[str],
) -> None:
    """Refuse a setting that would reach a protected section, before anything is rendered.

    Both are walked in sorted order rather than the order they arrived in. `disabled` reaches
    here as a set, whose iteration order moves with the interpreter's hash seed, so the first
    bad id found -- and therefore the fix the person is told about -- would otherwise change
    between two restarts on the same settings. An error two people cannot compare is most of
    an error wasted.
    """
    known = {section.id: section for section in sections}
    for section_id in sorted(overrides):
        if not _find(known, section_id).overridable:
            message = (
                f"The {section_id} section cannot be overridden; it is one of the two parts "
                f"of the prompt no setting may reach. Remove {section_id!r} from the overrides."
            )
            raise LucyError(PROTECTED_SECTION, message)
    for section_id in sorted(disabled):
        if not _find(known, section_id).disableable:
            message = (
                f"The {section_id} section cannot be disabled; it is one of the two parts of "
                f"the prompt no setting may reach. Remove {section_id!r} from the disabled list."
            )
            raise LucyError(PROTECTED_SECTION, message)


def _body(title: str, text: str) -> str:
    """Heading and text joined the one way, so every measurement is of what actually ships."""
    return f"## {title}\n\n{text}"


def _shrink_lines(text: str, affordable: Affordable) -> tuple[str, str]:
    """Prose gives up its last lines: the first carries the sentence that matters.

    The counts are of the section's own lines and never of the heading, because the person
    being told "showing 48 of 202" wrote 200 of them and cannot reconcile the other two.
    """
    lines = text.split("\n")
    prefixes = accumulate(lines, lambda kept, line: f"{kept}\n{line}")
    fitting = list(takewhile(affordable, prefixes))
    return (fitting[-1] if fitting else ""), f"showing {len(fitting)} of {len(lines)} lines"


def _fit(
    section: PromptSection,
    context: PromptContext,
    text: str,
    counter: Counter,
    *,
    shrink: Shrinker | None,
) -> Section:
    """One section, shortened to its ceiling if it has to be, and never quietly.

    A shortened section keeps its heading and gains the counts, so the confession arrives
    attached to the thing it is about and a section that could not fit one line is still a
    section that says so rather than a gap nobody can see. How it is shortened is the
    section's own business: `shrink` when it renders a structure, lines from the end when it
    is prose. A section nobody may override is given a floor equal to its whole length -- if
    a person cannot turn it off, the trimmer may not deliver half of it either.
    """
    body = _body(section.title, text)
    tokens = counter.count(body)
    whole = Section(
        id=section.id,
        band=section.band,
        body=body,
        tokens=tokens,
        priority=section.priority,
        floor_tokens=0 if section.overridable else tokens,
        title=section.title,
    )
    if tokens <= section.max_tokens:
        return whole

    def affordable(candidate: str) -> bool:
        return fits(_body(section.title, candidate), section.max_tokens, counter)

    kept, detail = shrink(context, affordable) if shrink else _shrink_lines(text, affordable)
    notice = f"{section.id} was shortened to its {section.max_tokens}-token ceiling: {detail}."
    shortened = _body(section.title, f"{kept}\n\n[{notice}]" if kept else f"[{notice}]")
    return whole.with_body(shortened, counter.count(shortened), notice=notice)


def render_all(
    context: PromptContext,
    *,
    overrides: Mapping[str, str] | None = None,
    disabled: Collection[str] | None = None,
    sections: Sequence[PromptSection] = BUILTIN,
    counter: Counter | None = None,
) -> tuple[Section, ...]:
    """Zones 0 and 1 as `Section`s, in prompt order, with their tokens already counted.

    A section that renders nothing is left out rather than emitted empty: an empty heading
    reads, to a model, as a thing that exists and has nothing in it. A section that rendered
    something and then could not fit is kept, with the notice, because that one is news.

    An override that is blank falls back to the default. A settings store that hands back an
    empty string almost always means "unset", and losing the safety-adjacent sections to a
    blank row is a worse failure than ignoring a person who meant it -- they have `disabled`.
    """
    replacements = overrides or {}
    switched_off = frozenset(disabled or ())
    _check(sections, replacements, switched_off)
    pricing = counter if counter is not None else ESTIMATE
    rendered: list[Section] = []
    for section in sections:
        if section.id in switched_off:
            continue
        replacement = replacements.get(section.id, "")
        text = replacement or section.render(context)
        if not text:
            continue
        # A replacement is prose somebody typed, not the structure the shrinker knows how to
        # take apart, so an overridden section is shortened the ordinary way even here.
        shrink = None if replacement else section.shrink
        rendered.append(_fit(section, context, text, pricing, shrink=shrink))
    return tuple(rendered)


def prompt_version(sections: Sequence[PromptSection] = BUILTIN) -> str:
    """Which prompt wrote this transcript, in one string a resumed session can compare.

    Every field of every section goes in, which is the only version of this that keeps its
    promise. The rendered default text is there so that editing a markdown file and
    forgetting to bump its version cannot produce two different prompts wearing the same
    name. So are the ceiling, the priority, the two protection flags and whether the section
    has a shrinker, because each of those decides what a turn was actually shown: a ceiling
    halved ships half a section, a priority changed gives a different section up first, and a
    flag flipped puts a section within reach of a setting that could not touch it yesterday.
    A digest blind to those would call the before and the after by the same name, and the
    name is all a resumed session has.
    """
    empty = PromptContext()
    material = "\n".join(
        f"{section.id}\x1f{section.version}\x1f{section.title}\x1f{section.band}"
        f"\x1f{section.priority}\x1f{section.max_tokens}"
        f"\x1f{section.overridable}\x1f{section.disableable}\x1f{section.shrink is not None}"
        f"\x1f{section.render(empty)}"
        for section in sections
    )
    return f"{PROMPT_VERSION}.{hashlib.sha256(material.encode()).hexdigest()[:12]}"


__all__ = [
    "BUILTIN",
    "ESTIMATE",
    "PACKAGE",
    "PROMPT_VERSION",
    "PROTECTED_SECTION",
    "UNKNOWN_SECTION",
    "Affordable",
    "PromptContext",
    "PromptSection",
    "Renderer",
    "Shrinker",
    "prompt_version",
    "render_all",
]
