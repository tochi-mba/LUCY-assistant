"""The live state block: what is true right now, rendered last so it costs only itself.

Lucy's prompt is ordered by volatility (`lucy_api.context.types` explains why), and this is
the one piece rewritten on every single turn. Anywhere but last it would end the provider's
cached prefix at its own offset and make every token before it full price again; placed
last it costs its own length and nothing else. That single fact decides the rest of this
module: the block is one `Section` in `Band.pinned`, it is regenerated from scratch rather
than patched, and it is therefore small enough to regenerate.

## Why these groups and not a dump of the session

Each group is a fact the model would otherwise guess at, spend a tool call on, or carry
forward from a turn that has since been compacted away, and guessing is expensive in a way
that is hard to see from the outside. A model that does not know the date invents one. A
model that cannot see its own position in the window writes its note after the eviction
instead of before it. A model that cannot see that an operation has already been refused
twice calls it a third time. None of those are reasoning failures. They are missing facts,
and facts are cheap.

## Why each group has its own ceiling and its own floor

One shared pool would let thirty running sub-agents evict the memory index, which is the
failure that makes an assistant look like it has forgotten what it knows. So each group is
capped on its own, and a capped group says so with counts -- "12 agents running (showing the
5 most recent)". Nothing here ever shows fewer things quietly: a model reading a short list
has no way to tell it from a complete one, and neither has anybody debugging it afterwards.

## Why a ladder and not a truncation

When the whole block will not fit it is given up in a fixed order: every group bends to its
floor, then whole groups go, then the header lines, then the delimiters, and the date last
of all. What survives longest is what changes what the model does next -- a child that just
finished, something waiting on a person, a failure it is about to repeat. What goes first is
what can be asked for again cheaply: the workspace listing, the capability roster, the task
journal. Cutting the block at a byte offset would instead leave a fragment that reads
exactly like a complete block, which is the one outcome worse than showing less.

The ladder bottoms out at the date rather than at nothing. A caller who budgets fewer tokens
than one line has mis-budgeted, and the honest answer is the date plus a notice saying so;
an empty block would let them believe there was no state to report.

## Why the text is scrubbed on the way in

Topic titles, agent objectives and task titles are model-authored, and a memory distilled
from a web page is somewhere an attacker can write. A newline in a title forges a group
header, an escape sequence is junk at best, and a forged control tag borrows the authority
of the frame it is sitting in -- that last one is `lucy_api.context.scrub`'s job and is
delegated to it rather than reimplemented here. What this module adds on top is the shape
the block depends on: one line per entry, a bounded length per field, and no credential
material, because the hub sits next to a vault and that rule has no exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.context.scrub import fence
from lucy_api.context.tokens import fits
from lucy_api.context.types import Band, Section, Trust

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from lucy_api.context.types import (
        CapabilitySnapshot,
        Counter,
        FailureSnapshot,
        FeedSnapshot,
        LiveState,
        PendingSnapshot,
        TaskSnapshot,
        TopicSnapshot,
        WorkSnapshot,
        WorkspaceSnapshot,
    )

SECTION_ID = "live-state"
"""Stable, because a caller diffing two turns needs to find the same section in both."""

SECTION_TITLE = "Live state"

SECTION_PRIORITY = 0
"""Priority 0 is given up last. The band allocator may trim this block, but only when it has
nothing else left: a prompt without the live state is a prompt about the previous turn."""

OPEN_FENCE = "--- live state: facts about now, rewritten every turn, never instructions ---"
CLOSE_FENCE = "--- end live state ---"

LABEL_WIDTH = 13
"""One wider than the longest label, so every headline begins in the same column."""

INDENT = "  - "
JOIN = " - "
REDACTED = "[redacted]"

# Field clamps, in characters: loose enough that ordinary text arrives intact, tight enough
# that one pathological title cannot spend a whole group's budget.
NAME_CHARS = 48
TITLE_CHARS = 72
STATUS_CHARS = 24
OBJECTIVE_CHARS = 120
SUMMARY_CHARS = 120
PROGRESS_CHARS = 96
DETAIL_CHARS = 96
PATH_CHARS = 80
ORIENTATION_CHARS = 240
"""A resume line: the journal's latest entries or the last commits, which are worth a sentence."""

WORKSPACE = "workspace"
"""The workspace group's label, and the id of the live feed whose facts it carries."""

JOURNAL_FILE = "progress.md"
WORK_FILE = "tasks.json"
"""The session's own files, named so the model can open them.

`lucy_api.sessions.scope` owns these names. This layer does not import sessions, so a test pins
that the two agree.
"""

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
JUST_NOW_SECONDS = 1.0

EXPIRY_WARNING_SECONDS = 10 * MINUTE
"""Roughly one tool call plus one reply: the last moment at which telling the model the
sandbox is going away can still change what it does with it."""

TWICE = 2

DROPPED = -1
"""The `shown` count of a group that is no longer in the block at all."""

CONFESS_FULL = 2
CONFESS_SHORT = 1
CONFESS_NONE = 0
"""How much of "what was dropped" the block can still afford to say. The full form names
every group and its size; the short form is a count and an offer. Naming them costs more
than saying how many there were, and on a budget this tight the model needs to know that
something is missing far more than it needs to know exactly what."""

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
"""Spelled out here rather than taken from `strftime`, whose weekday names follow the
process locale. Same state in, same bytes out, on every machine that runs the hub."""

NO_TIMEZONE = "no timezone recorded"
"""Said out loud rather than left blank, because a bare timestamp is one the model reads as
its own. It covers both the moment that carries no zone and the zone that names itself
nothing: in the text the model gets, those are the same fact."""

_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_RUN_OF_SPACE = re.compile(r"\s+")

# High-signal shapes only. A pattern that fired on ordinary prose would quietly delete text a
# person wrote, so every branch here needs either a published prefix or an explicit
# `key: value` spelling before it matches anything.
_SENSITIVE = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{12,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
    r"|(?i:\b(?:api[_-]?key|secret|token|password|passwd|bearer)\b[ \t]*[:=][ \t]*\S+)"
)


@dataclass(frozen=True, slots=True)
class _Quota:
    """One group's share of the block: when it is given up, and how far it bends first."""

    rank: int
    ceiling: int
    floor: int


# Read this as one policy statement rather than as eight numbers. `rank` is the order things
# are surrendered in, highest first, and it is the only place the argument is made.
#
# The principle is not importance, it is what the model cannot recover on its own. A
# workspace listing (80) or a capability list (70) can be asked for again for the price of
# one tool call, so they go first. In-flight work goes last: a child that just finished (10)
# is the only announcement of a result nobody will repeat, and what is waiting on a human
# (20) is the difference between stopping and spinning.
#
# Running children (40) outlive the memory index (50) for the same reason, and that ordering
# is deliberate rather than incidental. Losing the index costs depth the model can go and
# fetch. Losing the roster costs it the knowledge that work is already under way, and a model
# that does not know a child is running will duplicate it, contradict it, or answer as though
# nothing were pending.
#
# `ceiling` is what a group may show when there is room; `floor` is how few entries still
# earn a header.
QUOTAS: Mapping[str, _Quota] = {
    "in_flight": _Quota(rank=40, ceiling=5, floor=2),
    "finished": _Quota(rank=10, ceiling=4, floor=2),
    "tasks": _Quota(rank=60, ceiling=6, floor=2),
    "memory": _Quota(rank=50, ceiling=8, floor=3),
    "workspace": _Quota(rank=80, ceiling=6, floor=0),
    "feeds": _Quota(rank=75, ceiling=8, floor=1),
    "capabilities": _Quota(rank=70, ceiling=6, floor=2),
    "pending": _Quota(rank=20, ceiling=5, floor=2),
    "trouble": _Quota(rank=30, ceiling=4, floor=2),
}


@dataclass(frozen=True, slots=True)
class _Group:
    """One labelled group: a headline that always renders, and entries that may not."""

    name: str
    quota: _Quota
    headline: str
    entries: tuple[str, ...]
    recent: bool = False
    """Whether the entries are ordered newest first. The confession says so out loud, and a
    group that is not ordered by time must not make that claim."""

    @property
    def size(self) -> str:
        """How many entries this group holds, when that is a number worth printing.

        A group whose content is its headline can legitimately hold no entries at all -- the
        workspace is a path, a readiness and an expiry, and its entries are only the file
        listing. Confessing that one as "workspace (0)" reports something genuinely dropped as
        something that held nothing, which is the line a reader stops reading at.
        """
        return f" ({len(self.entries)})" if self.entries else ""

    def lines(self, shown: int) -> tuple[str, ...]:
        """The group at a given size, with the confession folded into its headline."""
        return (_label(self.name) + self.headline + self._confession(shown), *self.entries[:shown])

    def _confession(self, shown: int) -> str:
        """What is missing, in the text the model reads, with the counts."""
        total = len(self.entries)
        if shown >= total:
            return ""
        if shown == 0:
            return f" (showing none of {total})"
        if self.recent:
            return f" (showing the {shown} most recent of {total})"
        return f" (showing {shown} of {total})"


@dataclass(slots=True)
class _Plan:
    """One rung of the give-up ladder: how much of the block is still standing."""

    head: int
    shown: list[int]
    fenced: bool = True
    confess: int = CONFESS_FULL


def render_state(state: LiveState, *, limit: int, counter: Counter) -> Section:
    """Render this turn's live state as the last section of the prompt.

    The result always says what it left out, and is byte-identical for identical input so
    that two turns can be diffed against one another.
    """
    head = (_now_line(state), _session_line(state), _context_line(state))
    groups = _groups(state)
    plan = _Plan(head=len(head), shown=[min(len(g.entries), g.quota.ceiling) for g in groups])
    body = _render(head, groups, plan)
    while not fits(body, limit, counter) and _give_up(groups, plan):
        body = _render(head, groups, plan)
    tokens = counter.count(body)
    notice = _notice(head, groups, plan)
    if tokens > limit:
        notice = _joined(
            notice, f"the block cannot go below {tokens} tokens and the budget was {limit}"
        )
    return Section(
        id=SECTION_ID,
        band=Band.pinned,
        body=body,
        tokens=tokens,
        priority=SECTION_PRIORITY,
        floor_tokens=counter.count(head[0]),
        title=SECTION_TITLE,
        truncated=bool(notice),
        notice=notice,
    )


def _render(head: Sequence[str], groups: Sequence[_Group], plan: _Plan) -> str:
    """The block as bytes, at whatever rung of the ladder the plan is standing on."""
    lines: list[str] = []
    if plan.fenced:
        lines.append(OPEN_FENCE)
    lines.extend(head[: plan.head])
    for group, shown in zip(groups, plan.shown, strict=True):
        if shown != DROPPED:
            lines.extend(group.lines(shown))
    gone = [group for group, shown in zip(groups, plan.shown, strict=True) if shown == DROPPED]
    if gone and plan.confess != CONFESS_NONE:
        lines.append(_label("omitted") + _omitted(gone, plan.confess))
    if plan.fenced:
        lines.append(CLOSE_FENCE)
    return "\n".join(lines)


def _give_up(groups: Sequence[_Group], plan: _Plan) -> bool:
    """Surrender the next cheapest thing, or report that there is nothing left to give.

    The order is this module's argument. Every group bends before any group breaks, because
    two agents and three topics beat five agents and no index. Only once every group is at
    its floor does a whole group go, worst rank first. The header lines outlive every group
    below them: the date, the session and the position are why the block exists at all, and
    they outlive the *names* of what was dropped, which is why the list of dropped groups is
    cut down to a count before a single header line goes.
    """
    if _bend_or_drop(groups, plan):
        return True
    if plan.confess == CONFESS_FULL and _anything_dropped(plan):
        plan.confess = CONFESS_SHORT
        return True
    if plan.head > 1:
        plan.head -= 1
        return True
    if plan.fenced:
        # The delimiters stop model-authored text from being read as structure. By this rung
        # every group carrying such text is already gone, so they buy nothing and the tokens
        # are better spent on the date.
        plan.fenced = False
        return True
    if plan.confess == CONFESS_SHORT:
        # Last, and reluctantly. What was dropped stays in the section's notice, so it is
        # still on the record for whoever is debugging -- just no longer in the prompt.
        plan.confess = CONFESS_NONE
        return True
    return False


def _bend_or_drop(groups: Sequence[_Group], plan: _Plan) -> bool:
    """Bend the worst group to its floor, or -- once they are all at their floor -- drop it."""
    index = _worst(groups, plan, only_above_floor=True)
    if index is not None:
        plan.shown[index] = groups[index].quota.floor
        return True
    index = _worst(groups, plan, only_above_floor=False)
    if index is not None:
        plan.shown[index] = DROPPED
        return True
    return False


def _anything_dropped(plan: _Plan) -> bool:
    return any(shown == DROPPED for shown in plan.shown)


def _omitted(gone: Sequence[_Group], confess: int) -> str:
    """The line that keeps a dropped group from vanishing, in one of its two sizes."""
    if confess == CONFESS_FULL:
        named = ", ".join(f"{group.name}{group.size}" for group in gone)
        return f"{named} - dropped for space, ask if you need them"
    return f"{_plural(len(gone), 'group', 'groups')} dropped for space, ask what is missing"


def _worst(groups: Sequence[_Group], plan: _Plan, *, only_above_floor: bool) -> int | None:
    """Which group to give up next, or None when there is no candidate left."""
    worst: int | None = None
    for index, (group, shown) in enumerate(zip(groups, plan.shown, strict=True)):
        if shown == DROPPED:
            continue
        if only_above_floor and shown <= group.quota.floor:
            continue
        if worst is None or group.quota.rank > groups[worst].quota.rank:
            worst = index
    return worst


def _notice(head: Sequence[str], groups: Sequence[_Group], plan: _Plan) -> str:
    """Everything the block left out, for the caller rather than for the model.

    `Section.notice` costs no tokens, so it is where the whole truth goes even on the rung
    where the block itself could no longer afford to state it.
    """
    parts: list[str] = []
    for group, shown in zip(groups, plan.shown, strict=True):
        if shown == DROPPED:
            parts.append(f"{group.name} omitted{group.size}")
        elif shown < len(group.entries):
            parts.append(f"{group.name} showing {shown} of {len(group.entries)}")
    if plan.head < len(head):
        parts.append(_plural(len(head) - plan.head, "header line", "header lines") + " omitted")
    if not plan.fenced:
        parts.append("delimiters omitted")
    if plan.confess == CONFESS_SHORT:
        parts.append("the list of omitted groups was shortened to a count")
    if plan.confess == CONFESS_NONE:
        parts.append("the list of omitted groups did not fit")
    if not parts:
        return ""
    return "live state shortened: " + ", ".join(parts)


# --------------------------------------------------------------------------------------
# The three lines that always render. Between them they answer "when is it", "who am I
# talking to" and "how much room is left", which is every question a model would otherwise
# open a tool call to ask, or quietly invent an answer to.
# --------------------------------------------------------------------------------------


def _now_line(state: LiveState) -> str:
    """Models hallucinate the date more reliably than they hallucinate anything else."""
    stamp = state.now.strftime("%Y-%m-%d %H:%M")
    return _label("now") + f"{stamp} {_zone(state.now)} ({DAYS[state.now.weekday()]})"


def _zone(moment: datetime) -> str:
    """A timestamp with no zone is one the model will silently read as its own.

    Cleaned like any model-authored field even though `now` is the hub's own clock, because
    `tzname` returns whatever the attached `tzinfo` decides to return and the date line is the
    one line here that nothing else cleans. A newline in that string forges a header of its
    own, above the real one. One call is cheaper than an argument about which `tzinfo` every
    caller will ever pass, and a zone that cleans away to nothing is a zone we cannot name.
    """
    name = moment.tzname()
    if name is None:
        return NO_TIMEZONE
    return _clean(name, NAME_CHARS) or NO_TIMEZONE


def _session_line(state: LiveState) -> str:
    """Which conversation this is, and under which rules it is being held."""
    session = state.session
    title = f'"{_clean(session.title, TITLE_CHARS)}"' if session.title else ""
    incognito = "incognito: nothing here is written to memory" if session.incognito else ""
    return _label("session") + _joined(
        _clean(session.id, NAME_CHARS),
        title,
        f"profile {_clean(session.profile, NAME_CHARS)}",
        f"turn {session.turn_number}",
        f"permission mode {_clean(session.permission_mode, NAME_CHARS)}",
        incognito,
    )


def _context_line(state: LiveState) -> str:
    """Where the model stands in its own window, which changes what it chooses to do."""
    budget = state.budget
    reclaimable = (
        f"{_plural(budget.reclaimable, 'tool result', 'tool results')} reclaimable"
        if budget.reclaimable
        else ""
    )
    compaction = (
        f"last compaction at turn {budget.last_compaction_turn}"
        if budget.last_compaction_turn is not None
        else ""
    )
    return _label("context") + _joined(
        f"{budget.used:,} of {budget.window:,} tokens ({budget.percent}% used)",
        reclaimable,
        compaction,
    )


# --------------------------------------------------------------------------------------
# The groups. Each builder assumes it has something to say, because deciding whether it does
# is `_groups`'s job: "agents: none" ten times over is ten lines that teach nothing.
# --------------------------------------------------------------------------------------


def _groups(state: LiveState) -> tuple[_Group, ...]:
    """Every group with something in it, in prompt order."""
    groups: list[_Group] = []
    running = sorted(state.running, key=lambda work: (work.elapsed_seconds, work.id))
    if running:
        groups.append(_in_flight_group(running))
    finished = sorted(
        (work for work in state.in_flight if work.finished_since_last_turn),
        key=lambda work: (-work.elapsed_seconds, work.id),
    )
    if finished:
        groups.append(_finished_group(finished))
    if state.tasks:
        groups.append(_tasks_group(state.tasks))
    if state.topics:
        groups.append(_memory_group(state.topics, state.now))
    feeds = [feed for feed in state.feeds if feed.lines]
    if state.workspace is not None:
        # The workspace's own feed is about the same place. Rendered as its own group it was
        # a second `workspace` line under the first, so its facts join this one's headline.
        facts = tuple(line for feed in feeds if feed.id == WORKSPACE for line in feed.lines)
        feeds = [feed for feed in feeds if feed.id != WORKSPACE]
        groups.append(_workspace_group(state.workspace, facts))
    if state.capabilities:
        groups.append(_capabilities_group(state.capabilities))
    groups.extend(_feed_group(feed) for feed in feeds)
    if state.pending.any:
        groups.append(_pending_group(state.pending))
    if state.failures:
        groups.append(_trouble_group(state.failures))
    return tuple(groups)


def _in_flight_group(running: Sequence[WorkSnapshot]) -> _Group:
    """Everything still going, in one group: helpers, jobs and commands together.

    Newest first, because the ones the model has not yet reasoned about are the news.
    """
    return _Group(
        name="in_flight",
        quota=QUOTAS["in_flight"],
        headline=_plural(len(running), "thing running", "things running"),
        entries=tuple(_work_line(work) for work in running),
        recent=True,
    )


def _finished_group(finished: Sequence[WorkSnapshot]) -> _Group:
    """The delta that drives the next move: work that ended while the model was away.

    Longest-running first when the group has to bend, on the reasoning that the helper which
    worked for four minutes has more to report than the one that stopped after four seconds.

    A line here says a thing finished and roughly how big the answer is. It never carries the
    answer: reading a result is a separate, explicit act, so a job that produced forty
    megabytes of log does not arrive uninvited.
    """
    return _Group(
        name="finished",
        quota=QUOTAS["finished"],
        headline=_plural(len(finished), "thing finished", "things finished")
        + " since your last turn",
        entries=tuple(_finished_line(work) for work in finished),
    )


def _tasks_group(tasks: Sequence[TaskSnapshot]) -> _Group:
    """The shared journal, in the order the journal keeps it.

    Re-sorting here would hide the ordering the siblings agreed on, and the journal is the
    only thing they share: no context passes between them, only these lines.
    """
    blocked = sum(1 for task in tasks if task.blocked_by)
    headline = _plural(len(tasks), "task in the shared journal", "tasks in the shared journal")
    if blocked:
        headline += f", {blocked} blocked"
    return _Group(
        name="tasks",
        quota=QUOTAS["tasks"],
        headline=headline,
        entries=tuple(_task_line(task) for task in tasks),
    )


def _memory_group(topics: Sequence[TopicSnapshot], now: datetime) -> _Group:
    """The index, never the memories.

    Carrying every memory would cost more than it is worth and bury the useful ones;
    carrying none leaves the model unable to know that it knows anything. A title, a
    sentence and a count are enough for it to decide that one topic is worth expanding.
    """
    relevance = any(topic.relevance_order is not None for topic in topics)
    ordered = sorted(
        topics,
        key=lambda topic: (
            topic.relevance_order if topic.relevance_order is not None else len(topics),
            _topic_order(topic, now),
        ),
    )
    unread = sum(topic.unread for topic in ordered)
    unconfirmed = sum(topic.unconfirmed for topic in ordered)
    headline = (
        f"{_plural(len(ordered), 'topic', 'topics')}, "
        f"{_plural(sum(topic.count for topic in ordered), 'memory', 'memories')}"
    )
    if ordered[0].index_notice:
        headline += "; " + ordered[0].index_notice
    if unread:
        headline += f", {unread} unread"
    if unconfirmed:
        # Said, not dropped: these exist and cannot be used until the person confirms them,
        # and a model that does not know they exist will tell the person it knows nothing.
        headline += f", {unconfirmed} unconfirmed"
    return _Group(
        name="memory",
        quota=QUOTAS["memory"],
        headline=headline,
        entries=tuple(_topic_line(topic, now) for topic in ordered),
        # "the 8 most recent of 40" is a claim about the ordering, and a topic nobody has
        # opened has no place in that ordering: it is sorted to the back and then tie-broken
        # on its title. Where even one topic is undated the group gives up the claim and
        # confesses with a plain count, because a cut made partly on alphabetical order that
        # calls itself recency is worse than one that admits it is just a count.
        recent=not relevance and all(_touched(topic, now) is not None for topic in ordered),
    )


def _workspace_group(workspace: WorkspaceSnapshot, facts: Sequence[str] = ()) -> _Group:
    """Where the work is happening, and what moved in it since the model last looked.

    Each thing once. `facts` are the workspace feed's lines -- shells, isolation, branch --
    and go in the headline, which always renders. The session's files are named as files the
    model can open, because the `tasks` group is the helpers' shared journal: a different
    thing under the same word.
    """
    ready = "ready" if workspace.ready else "not ready"
    changed = (
        f"{_plural(len(workspace.changed_files), 'file', 'files')} changed since your last turn"
        if workspace.changed_files
        else ""
    )
    oriented = tuple(
        INDENT + _clean(line, ORIENTATION_CHARS)
        for line in (
            _git_line(workspace),
            f"{JOURNAL_FILE}, latest: {workspace.journal}" if workspace.journal else "",
            f"{WORK_FILE}: {workspace.tasks}" if workspace.tasks else "",
        )
        if line
    )
    listing = tuple(INDENT + _clean(name, PATH_CHARS) for name in workspace.changed_files)
    return _Group(
        name=WORKSPACE,
        quota=QUOTAS[WORKSPACE],
        headline=_joined(
            _clean(workspace.path, PATH_CHARS),
            ready,
            *(_clean(fact, DETAIL_CHARS) for fact in facts),
            changed,
            _expiry(workspace.expires_in_seconds),
        ),
        entries=(*oriented, *listing),
    )


def _git_line(workspace: WorkspaceSnapshot) -> str:
    """The last few commits, or that there is no git to ask."""
    if workspace.git_missing:
        return "git is not available in this sandbox"
    return "recent commits: " + "; ".join(workspace.commits) if workspace.commits else ""


def _capabilities_group(capabilities: Sequence[CapabilitySnapshot]) -> _Group:
    """What can be done now and -- the part that changes behaviour -- what just changed."""
    ordered = sorted(capabilities, key=lambda capability: (not capability.changed, capability.id))
    ready = sum(1 for capability in ordered if capability.state == "ready")
    changed = sum(1 for capability in ordered if capability.changed)
    headline = f"{ready} ready of {len(ordered)}"
    if changed:
        headline += f", {changed} changed since your last turn"
    return _Group(
        name="capabilities",
        quota=QUOTAS["capabilities"],
        headline=headline,
        entries=tuple(_capability_line(capability) for capability in ordered),
    )


def _feed_group(feed: FeedSnapshot) -> _Group:
    """One sibling's live facts, labelled with the product name the model already knows."""
    return _Group(
        name=feed.id[:LABEL_WIDTH],
        quota=QUOTAS["feeds"],
        headline=feed.title,
        entries=tuple(INDENT + _clean(line, DETAIL_CHARS) for line in feed.lines),
    )


def _pending_group(pending: PendingSnapshot) -> _Group:
    """Everything blocked on somebody else, so the model stops instead of spinning."""
    entries = (
        *(_pending_line("waiting for approval", item) for item in pending.approvals),
        *(_pending_line("waiting for an answer", item) for item in pending.elicitations),
        *(_pending_line("waiting to connect", item) for item in pending.connections),
    )
    return _Group(
        name="pending",
        quota=QUOTAS["pending"],
        headline=_plural(
            len(entries), "thing waiting on somebody else", "things waiting on somebody else"
        ),
        entries=entries,
    )


def _trouble_group(failures: Sequence[FailureSnapshot]) -> _Group:
    """The cheapest loop-breaker there is: the model can see that it has already tried."""
    ordered = sorted(failures, key=lambda failure: (-failure.count, failure.operation))
    return _Group(
        name="trouble",
        quota=QUOTAS["trouble"],
        headline=_plural(
            len(ordered), "operation failing repeatedly", "operations failing repeatedly"
        ),
        entries=tuple(_failure_line(failure) for failure in ordered),
    )


# --------------------------------------------------------------------------------------
# Entry lines. Every field is scrubbed on the way in, and an empty field disappears rather
# than rendering as an empty slot: "  -  - 2m14s" reads like a bug in the assistant.
# --------------------------------------------------------------------------------------


def _work_line(work: WorkSnapshot) -> str:
    role = _clean(work.role, NAME_CHARS)
    if work.depth > 1:
        role += f" (depth {work.depth})"
    return INDENT + _joined(
        role,
        _clean(work.objective, OBJECTIVE_CHARS),
        _duration(work.elapsed_seconds),
        _clean(work.progress, PROGRESS_CHARS),
    )


def _finished_line(work: WorkSnapshot) -> str:
    return INDENT + _joined(
        _clean(work.role, NAME_CHARS),
        _clean(work.objective, OBJECTIVE_CHARS),
        f"{_clean(work.status, STATUS_CHARS)} after {_duration(work.elapsed_seconds)}",
        _clean(work.progress, PROGRESS_CHARS),
    )


def _task_line(task: TaskSnapshot) -> str:
    claimed = (
        f"claimed by {_clean(task.claimed_by, NAME_CHARS)}" if task.claimed_by else "unclaimed"
    )
    blocked = (
        "blocked by " + ", ".join(_clean(other, NAME_CHARS) for other in task.blocked_by)
        if task.blocked_by
        else ""
    )
    return INDENT + _joined(
        _clean(task.title, TITLE_CHARS), _clean(task.status, STATUS_CHARS), claimed, blocked
    )


def _topic_line(topic: TopicSnapshot, now: datetime) -> str:
    counts = _plural(topic.count, "memory", "memories")
    if topic.unread:
        counts += f", {topic.unread} unread"
    if topic.unconfirmed:
        counts += f", {topic.unconfirmed} unconfirmed"
    trust = "" if topic.trust == Trust.stated else f"trust: {_clean(topic.trust, STATUS_CHARS)}"
    # Named as `notes.openTopic` names its input. Left out, the index offered a topic to
    # expand and no way to ask for it: the model guessed an id and was told "not found".
    return INDENT + _joined(
        _clean(topic.title, TITLE_CHARS),
        _clean(topic.summary, SUMMARY_CHARS),
        counts,
        _seen(topic.last_seen, now),
        trust,
        f"topic_id {_clean(topic.id, NAME_CHARS)}",
    )


def _capability_line(capability: CapabilitySnapshot) -> str:
    changed = "changed since your last turn" if capability.changed else ""
    return INDENT + _joined(
        _clean(capability.title, TITLE_CHARS),
        _clean(capability.state, STATUS_CHARS),
        changed,
        _clean(capability.detail, DETAIL_CHARS),
    )


def _pending_line(wait: str, item: str) -> str:
    return f"{INDENT}{wait}: {_clean(item, DETAIL_CHARS)}"


def _failure_line(failure: FailureSnapshot) -> str:
    line = f"{_clean(failure.operation, NAME_CHARS)} failed {_times(failure.count)}"
    if failure.detail:
        line += f": {_clean(failure.detail, DETAIL_CHARS)}"
    return INDENT + line


# --------------------------------------------------------------------------------------
# Formatting. Small, deliberate and shared, so that no two groups describe the same quantity
# two different ways inside one block.
# --------------------------------------------------------------------------------------


def _label(name: str) -> str:
    return f"{name:<{LABEL_WIDTH}}"


def _joined(*fields: str) -> str:
    """Join the fields that have something in them and drop the ones that do not."""
    return JOIN.join(field for field in fields if field)


def _plural(count: int, singular: str, plural: str) -> str:
    """'1 topic' and '4 topics': a block a model reads should read like English."""
    return f"{count} {singular if count == 1 else plural}"


def _times(count: int) -> str:
    if count == 1:
        return "once"
    if count == TWICE:
        return "twice"
    return f"{count} times"


def _duration(seconds: float) -> str:
    """Two units at most. '2m14s' is read at a glance; '134.19 seconds' is arithmetic."""
    total = int(seconds)
    if total < MINUTE:
        return f"{total}s"
    if total < HOUR:
        return f"{total // MINUTE}m{total % MINUTE:02d}s"
    if total < DAY:
        return f"{total // HOUR}h{total % HOUR // MINUTE:02d}m"
    return f"{total // DAY}d{total % DAY // HOUR:02d}h"


def _expiry(seconds: float | None) -> str:
    """A sandbox about to go away is only worth saying while there is still time to act."""
    if seconds is None:
        return ""
    if seconds <= 0:
        # The field is a deadline minus a clock, so it goes negative the instant the deadline
        # passes and stays negative until somebody notices. "expires in -90s" still reads as a
        # countdown, which is the opposite of what happened, and a model that reads it will go
        # on writing into a sandbox that is already gone.
        return "sandbox has expired: nothing written there now will survive"
    if seconds <= EXPIRY_WARNING_SECONDS:
        return f"sandbox expires in {_duration(seconds)}: save anything worth keeping now"
    return f"sandbox expires in {_duration(seconds)}"


def _elapsed(moment: datetime, now: datetime) -> float | None:
    """Seconds between the two, or None when they cannot honestly be compared.

    A naive timestamp and an aware one cannot be subtracted, and the live state block is the
    last place in the hub that should raise: whoever handed us a mismatched pair still
    deserves their date back.
    """
    if (moment.tzinfo is None) != (now.tzinfo is None):
        return None
    return (now - moment).total_seconds()


def _seen(moment: datetime | None, now: datetime) -> str:
    """How recently a topic was touched, which is most of how much it is worth."""
    if moment is None:
        return ""
    elapsed = _elapsed(moment, now)
    if elapsed is None:
        return f"last seen {moment.strftime('%Y-%m-%d')}"
    if elapsed < JUST_NOW_SECONDS:
        return "last seen just now"
    return f"last seen {_duration(elapsed)} ago"


def _touched(topic: TopicSnapshot, now: datetime) -> float | None:
    """Seconds since this topic was last read or written, or None when it has no usable date.

    Undated and unusably dated collapse to the same answer on purpose. A topic with no
    `last_seen` and one whose `last_seen` cannot be compared with `now` are both topics whose
    position in a list ordered by recency would be invented, and inventing it is the thing
    every caller of this is trying not to do.
    """
    return None if topic.last_seen is None else _elapsed(topic.last_seen, now)


def _topic_order(topic: TopicSnapshot, now: datetime) -> tuple[int, float, str]:
    """Most recently touched first, anything undated last, ties broken on the title."""
    elapsed = _touched(topic, now)
    if elapsed is None:
        return (1, 0.0, topic.title)
    return (0, elapsed, topic.title)


def _clean(text: str, chars: int) -> str:
    """Make model-authored text safe to put inside a structured block, and short enough.

    The order matters. Escape sequences and control characters go first, because a newline
    would forge a group header and an escape sequence is junk in a token stream. Credential
    shapes go next, before any clamp can leave the front half of one behind. Harness
    imitation is `scrub.fence`'s job, and it is used rather than repeated: this block
    delimits itself, so the marker `scrub.scrub` would add would be a second frame inside
    the first. The clamp is last, and it is what keeps one entry to one line of budget.
    """
    plain = _RUN_OF_SPACE.sub(" ", _CONTROL.sub(" ", _ESCAPE.sub("", text))).strip()
    plain = fence(_SENSITIVE.sub(REDACTED, plain))
    if len(plain) > chars:
        return plain[: chars - 3].rstrip() + "..."
    return plain


__all__ = ["SECTION_ID", "SECTION_PRIORITY", "SECTION_TITLE", "render_state"]
