"""Memory as an index of topics, because twenty loose sentences are not knowledge.

A memory store that hands the model a flat list of facts fails twice over. It cannot be
summarised -- there is nothing to summarise *to*, only more sentences -- and a model handed
twenty unrelated ones cannot tell what it knows *about*, only what it has been told. So
memories are clustered into topics, and what Lucy carries every turn is the topic index:
titles, one-line summaries, counts. Never the contents. The model reads the index, sees
that there is a topic called "home network" with eleven memories in it, and asks for that
one when it matters.

That is progressive disclosure applied to memory, and it is the only thing that makes an
always-current memory affordable. The index costs a line per topic, every turn, forever.
The memories cost whatever they cost and are paid for only when they are wanted.

## Recency decays from last access, not from creation

The obvious ranking -- newest first -- evicts precisely the memories worth keeping. A
person's home timezone is written once and used constantly; yesterday's note about a hotel
is written late and used never. Rank by creation and the timezone looks ancient while the
hotel looks current, which is the failure that makes an assistant feel like it forgets the
things you tell it most often. So the decay here runs from `last_seen`, which the memory
service stamps on *access*, and a fact that keeps being useful keeps looking new.
`first_seen` is carried for display and audit and is deliberately part of no score.

## A score is only meaningful against the set it was computed over

Recency, importance and unread count are three different quantities in three different
units, and adding them raw means whichever happens to carry the largest numbers decides
the ranking on its own. Each is min-max normalised across the candidate set first, so each
contributes a *position* between zero and one rather than a magnitude. The consequence is
the honest one: a score means nothing outside the call that produced it. It is not stored
on a topic, never persisted, and two scores from two calls over different candidates are
not comparable and must not be compared.

## Untrusted memories are not in the index

A memory store is a prompt-injection *persistence* layer. An attacker who lands one
sentence in a page that Lucy distils into a memory has written into every future
conversation, which is a far better return than landing it in one. Permanence is the whole
feature and therefore the whole risk. So anything marked `untrusted` is excluded from the
index by default, has to be confirmed by a person before it can enter, and the number held
back is reported rather than quietly dropped.

That check has to fail closed, because the topics it judges are decoded from another
service's JSON and trust is exactly the field a decoder gets slightly wrong: a bare
`"untrusted"` string, or a word from a newer memory service this one has never heard of.
So `Topic` resolves `trust` once, on the way in, and anything it cannot recognise becomes
`untrusted` rather than trusted by accident. That one coercion at the boundary is also
what makes every identity comparison below sound.

## Similarity is token overlap, on purpose

`assign_topic` decides which topic a new memory joins, and it decides it with Jaccard
overlap over normalised words. That is weaker than an embedding and it is chosen anyway:
it needs no model, no dependency and no network on the write path, it is deterministic,
and when it files something surprisingly a person can read the two strings and see why. An
embedding backend can replace `jaccard` behind this same signature later, because
`assign_topic` returns which topic it chose and how alike the two looked -- one number,
never a vector -- so no caller comes to depend on how that number was arrived at.

Similarity is consulted only where nobody has already decided. A key on the candidate is
somebody's decision, and a decision outranks a measurement: see `assign_topic`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol

from lucy_api.context.types import TopicSnapshot, Trust

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from lucy_api.context.types import Counter


HALF_LIFE_DAYS = 14.0
"""How long an untouched topic takes to lose half its recency.

A fortnight is the span over which a person's own sense of "recently" seems to work: last
week still counts, last quarter does not. It is a tuning constant, not a law, and it lives
here as one name rather than scattered through the arithmetic so that moving it is one
edit and one test.
"""

SIMILARITY_THRESHOLD = 0.4
"""How much word overlap makes two memories the same subject.

Two shared words in five, after stopwords are removed. Lower, and unrelated notes that
happen to share a proper noun get merged, which is the worse failure: a wrongly merged
topic hides both memories behind one misleading summary, while a wrongly split one costs a
line in the index and is visible to anybody reading it.
"""

RECENCY_WEIGHT = 0.5
IMPORTANCE_WEIGHT = 0.35
UNREAD_WEIGHT = 0.15
"""The three weights, summing to one.

Recency leads because the index exists to say what is live right now. Importance is close
behind because it is the one signal a person sets deliberately and it must not be drowned.
Unread is a nudge rather than a driver: something never surfaced deserves a look, but a
backlog of unread trivia must not push out the facts actually in use.
"""

SCORE_PRECISION = 9
"""Places the score is rounded to before ordering.

Two topics whose scores differ in the last bit of a float are not actually different, and
letting that bit decide means the order changes when an unrelated topic joins the set. The
documented tie-break is the better answer, so the score is blunted enough to reach it.
"""

KEY_WORDS = 4
"""Words taken from a memory when it has to name a topic of its own."""

SECONDS_PER_DAY = 86400.0

_WORDS = re.compile(r"[a-z0-9]+")

# A word table, kept as a table: one word per line would be forty lines of noise in the
# middle of the module, and the thing a reader needs to do with this is skim it.
# fmt: off
_STOPWORDS = frozenset({
    "a", "about", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "his", "i", "in", "is", "it", "its", "me",
    "my", "not", "of", "on", "or", "our", "she", "that", "the", "their", "them",
    "they", "this", "to", "was", "we", "were", "will", "with", "you", "your",
})
# fmt: on
"""Words whose overlap is not evidence.

Every English sentence shares these, so counting them makes every pair of memories look
related and pushes the threshold towards meaninglessness. The list is short and literal
rather than a dependency: a linguistics package would bring a data file, a download and a
version to pin, to solve a problem that is forty words wide.
"""


class UnknownTopicError(LookupError):
    """A topic that does not exist, answered as an error rather than as no memories.

    Silence would be read as "this topic is empty", and a model that believes it checked a
    topic and found nothing behaves very differently from one that knows it asked a wrong
    question. So the failure names what was asked for and what exists instead.
    """

    def __init__(self, topic_id: str, known: Sequence[str]) -> None:
        listed = ", ".join(repr(candidate) for candidate in known) or "no topics yet"
        super().__init__(f"Unknown topic {topic_id!r}; this profile has {listed}")
        self.topic_id = topic_id


_TRUST_BY_VALUE = {member.value: member for member in Trust}


def _as_trust(value: Trust | str) -> Trust:
    """One trust level resolved to a member, with anything unrecognised treated as unsafe.

    Fail closed, and deliberately without raising. A topic arrives across a service
    boundary, so `trust` can be whatever JSON carried: a bare string, a level a newer
    memory service knows about, a typo. Refusing the whole topic would lose a memory over a
    field it does not get to decide for itself, and defaulting to `stated` would hand the
    index to whoever controls the payload. Demoting is the reading that is wrong in the
    safe direction: an unrecognised level asks a person before it reaches the model.
    """
    return _TRUST_BY_VALUE.get(str(value), Trust.untrusted)


@dataclass(frozen=True, slots=True)
class Topic:
    """One cluster of memories, as the hub holds it.

    Richer than the `TopicSnapshot` it converts to, because ranking needs numbers the model
    is never shown: a model told its own importance weights will argue with them.
    """

    id: str
    key: str
    title: str
    summary: str = ""
    count: int = 0
    importance: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    trust: Trust = Trust.stated
    unread: int = 0
    unconfirmed: int = 0
    """Members held back until the person confirms them. Reported, never ranked on.

    Deliberately not a ranking input. These came from a page or a tool, and a topic must
    not climb the index because somebody else wrote a lot of unvouched things into it --
    that is the persistence attack the trust boundary exists to stop, pointed at the order
    instead of the content.
    """

    def __post_init__(self) -> None:
        """Resolve `trust` once, here, so that nothing downstream has to distrust it.

        The annotation says `Trust`; a decoder handing this class the string it read says
        otherwise, and `"untrusted" is Trust.untrusted` is `False`. Rather than spread
        value comparisons through every place trust is read -- where one omission fails
        open and nothing catches it -- the value is resolved where it enters.
        """
        object.__setattr__(self, "trust", _as_trust(self.trust))

    @property
    def trusted(self) -> bool:
        """Whether this topic may enter the index without a person confirming it first.

        An identity comparison is sound here only because `__post_init__` has already
        resolved the field to a member. It is not sound on a raw value, which is why the
        raw value does not survive construction.
        """
        return self.trust is not Trust.untrusted

    def to_snapshot(self) -> TopicSnapshot:
        """The index line's worth of this topic, and nothing else.

        `key`, `importance` and `first_seen` are dropped here rather than rendered small:
        the snapshot is what reaches the model, and every field it carries is a field
        somebody will later be tempted to have the model reason about.
        """
        return TopicSnapshot(
            id=self.id,
            title=self.title,
            summary=self.summary,
            count=self.count,
            last_seen=self.last_seen,
            trust=self.trust.value,
            unread=self.unread,
            unconfirmed=self.unconfirmed,
        )

    def text(self) -> str:
        """What similarity may read: the topic's own words, never its memories."""
        return f"{self.key} {self.title} {self.summary}"


@dataclass(frozen=True, slots=True)
class Memory:
    """One remembered fact, with where it came from still attached.

    Provenance travels with the memory all the way to the renderer. Dropping it to save
    tokens would turn a reported claim into an assertion Lucy appears to be making, which
    is the exact conversion an injected memory is hoping for.
    """

    id: str
    topic_id: str
    body: str
    source: str = ""
    trust: Trust = Trust.stated
    recorded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """A memory that has not been filed yet.

    No id, because it does not have one until it is stored, and a type that cannot hold a
    blank id is a type that cannot be filed before it is written.
    """

    body: str
    key: str = ""
    """A key the writer already knows, when it knows one -- a settled subject such as
    `home.timezone`. Present means "file it here"; absent means "work it out"."""

    def text(self) -> str:
        """The words similarity compares against an existing topic's.

        The body alone. A candidate carrying a key never reaches the comparison, so adding
        the key here could only drag a keyless memory towards a topic whose name reads like
        it, and a name is not evidence about a fact.
        """
        return self.body


AssignmentReason = Literal["key", "similarity", "new"]


@dataclass(frozen=True, slots=True)
class Assignment:
    """Where a new memory goes, and on what grounds.

    The grounds are returned rather than logged because filing is the step that goes wrong
    invisibly: a memory in the wrong topic is not lost, it is worse than lost, and the only
    cheap way to notice is for the caller to be able to see that a note about a dentist
    joined "home network" on a similarity of 0.41.
    """

    key: str
    reason: AssignmentReason
    topic: Topic | None = None
    similarity: float = 0.0


@dataclass(frozen=True, slots=True)
class Ranked:
    """One topic with its score and the three parts that made it.

    The parts are kept so that "why is this topic in the index and that one not" has an
    answer that is not "read the source". They are set-relative, like the score itself.
    """

    topic: Topic
    score: float
    recency: float
    importance: float
    unread: float


@dataclass(frozen=True, slots=True)
class Selection:
    """What made the cut, what did not, what was never eligible, and what needs a person.

    Separate numbers rather than one, because they mean different things to the person
    reading the index. `omitted` is "there was no room"; `withheld` is "this needs your
    say-so"; `unconfirmed` is "this is in front of you and still needs your say-so".
    Collapsing any of them into a single count would let an unconfirmed memory hide inside
    a budget notice, which is the one place it must never be able to hide.
    """

    topics: tuple[Topic, ...] = ()
    omitted: int = 0
    withheld: int = 0
    tokens: int = 0

    @property
    def considered(self) -> int:
        """How many topics were eligible, shown or not: the notice's denominator."""
        return len(self.topics) + self.omitted

    @property
    def unconfirmed(self) -> int:
        """How many of the chosen topics a person has not confirmed.

        Zero for every caller that did not ask for them, which is every caller but one.
        Derived from the topics themselves rather than recorded by the cut, so there is no
        way to build a selection that carries unconfirmed material and reports that it
        does not.
        """
        return sum(1 for topic in self.topics if not topic.trusted)

    @property
    def notice(self) -> str:
        """The confession, with exact counts, or empty when there is nothing to confess."""
        parts: list[str] = []
        if self.omitted:
            parts.append(f"showing {len(self.topics)} of {self.considered} topics")
        if self.unconfirmed:
            # Asking for unconfirmed topics is a reasonable thing for a confirmation screen
            # to do and a catastrophic thing for the assembler to do. Saying so here means
            # that if the second ever happens the prompt itself admits it, rather than the
            # mistake being visible only to somebody reading trust fields one by one.
            shown = _plural(self.unconfirmed, "unconfirmed topic")
            parts.append(f"{shown} shown for confirmation")
        if self.withheld:
            held = _plural(self.withheld, "unconfirmed topic")
            parts.append(f"{held} held back until confirmed")
        return "; ".join(parts)

    def snapshots(self) -> tuple[TopicSnapshot, ...]:
        """The chosen topics in the shape the live state block takes them."""
        return tuple(topic.to_snapshot() for topic in self.topics)


class TopicSource(Protocol):
    """The reads the hub needs from the memory service.

    A Protocol at the seam, because Memory-api is a separate repository and still a
    skeleton: the hub has to be finished, tested and reviewable before that service answers
    anything. A Protocol also means the composition root depends on the shape rather than
    on a client, so `FakeTopics` substitutes without a network and a change to this
    interface fails to type-check rather than passing quietly.

    Read-only on purpose. Writing a memory is a separate seam with a separate audience:
    this one sits on the turn's critical path and has to stay cheap enough to call every
    turn.
    """

    async def list_topics(self, account_id: str, *, profile: str) -> tuple[Topic, ...]:
        """Every topic for one person on one profile, untrusted ones included.

        Untrusted topics come back because the caller that has to confirm them needs to see
        them. `select_topics` is what keeps them out of the index.
        """
        ...

    async def read_topic(
        self, account_id: str, topic_id: str, *, profile: str
    ) -> tuple[Memory, ...]:
        """The memories in one topic: the expansion the index exists to make possible.

        Raises:
            UnknownTopicError: no such topic on this profile.
        """
        ...


class FakeTopics:
    """An in-memory stand-in satisfying the real Protocol, in the family's fake idiom.

    Hand-written rather than generated or mocked, so the type checker fails the build when
    the Protocol and the fake drift apart, and so a test reads as a description of a memory
    store rather than as a list of patched attributes. Deterministic: it returns what was
    seeded, in seeding order, every time, because a test of a ranking is worthless if the
    input order is a coin toss.
    """

    def __init__(self) -> None:
        self._topics: dict[tuple[str, str], tuple[Topic, ...]] = {}
        self._memories: dict[str, tuple[Memory, ...]] = {}
        self.lists = 0
        """How many times `list_topics` was called: one whole index, however wide."""

        self.reads = 0
        """How many times `read_topic` was called: the cost progressive disclosure saves."""

    def seed(self, account_id: str, *, profile: str, topics: Iterable[Topic]) -> None:
        """Set one person's topics on one profile, replacing whatever was there."""
        self._topics[(account_id, profile)] = tuple(topics)

    def seed_memories(self, topic_id: str, memories: Iterable[Memory]) -> None:
        """Set the contents of one topic."""
        self._memories[topic_id] = tuple(memories)

    async def list_topics(self, account_id: str, *, profile: str) -> tuple[Topic, ...]:
        """The seeded topics, or none: a person with no memories is not an error."""
        self.lists += 1
        return self._topics.get((account_id, profile), ())

    async def read_topic(
        self, account_id: str, topic_id: str, *, profile: str
    ) -> tuple[Memory, ...]:
        """The seeded memories of a topic that exists on this profile.

        Raises:
            UnknownTopicError: no such topic on this profile.
        """
        self.reads += 1
        known = self._topics.get((account_id, profile), ())
        if not any(topic.id == topic_id for topic in known):
            raise UnknownTopicError(topic_id, [topic.id for topic in known])
        return self._memories.get(topic_id, ())


if TYPE_CHECKING:
    # Static proof that the fake really does satisfy the Protocol. mypy checks `src` only,
    # so asserting this in the test suite would assert nothing; asserting it here costs a
    # type check and no runtime at all.
    _proof: TopicSource = FakeTopics()


def _plural(count: int, noun: str) -> str:
    """`noun` counted, pluralised by adding an s, which is all these nouns need."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _tokens(text: str) -> list[str]:
    """The comparable words of a string, in the order they were written.

    A list rather than a set because order is needed downstream, and because iterating a
    set of strings is only stable within a single process: a key derived from one would
    differ between runs, which is a bug that stays hidden until it is in production.
    """
    return [word for word in _WORDS.findall(text.lower()) if word not in _STOPWORDS]


def normalise(text: str) -> frozenset[str]:
    """The distinct comparable words of a string.

    Case and punctuation go because "Wi-Fi" and "wifi" are the same subject, and repetition
    goes because saying a word twice does not make two memories more alike.
    """
    return frozenset(_tokens(text))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Shared words over total words: 1.0 for identical, 0.0 for disjoint.

    An empty side scores zero rather than raising or returning one. A memory made entirely
    of stopwords resembles nothing, and the honest answer to "how alike are these" when one
    of them has no content is "not at all", not "perfectly".
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def assign_topic(candidate: Candidate, existing: Iterable[Topic]) -> Assignment:
    """Which topic a new memory joins: the key it was given, or else the words it shares.

    A key settles the question on its own, so the only two answers available to a candidate
    that has one are the topic already carrying it and the new topic that will. Measuring
    similarity as well would mean a memory keyed `home.wifi` joining `home.network` today
    because the wording happened to overlap and starting `home.wifi` tomorrow because it
    did not: the same fact in two topics depending on how it was phrased, which is the
    failure a key exists to prevent. Folding one key into another is a decision about the
    taxonomy, and it belongs to whoever owns the taxonomy rather than to a score computed
    on the write path.

    Without a key there is nothing to honour and the words are all there is. Ties in that
    pass go to whichever topic the source listed first. That is the source's own order, it
    is stable, and choosing arbitrarily among equals is how a store ends up with two topics
    that swap places between turns.

    An embedding backend replaces `jaccard` here without changing this signature; see the
    module docstring for why the cheap version is the one that shipped.
    """
    topics = tuple(existing)
    if candidate.key:
        for topic in topics:
            if topic.key == candidate.key:
                return Assignment(key=topic.key, reason="key", topic=topic, similarity=1.0)
        # Nobody holds that key yet, so this memory is the topic that will hold it. No
        # similarity is reported because none was measured, not because nothing resembled
        # it: a number here would be a reason, and the reason was the key.
        return Assignment(key=candidate.key, reason="new")

    words = normalise(candidate.text())
    best: Topic | None = None
    best_score = 0.0
    for topic in topics:
        score = jaccard(words, normalise(topic.text()))
        if score > best_score:
            best = topic
            best_score = score

    if best is not None and best_score >= SIMILARITY_THRESHOLD:
        return Assignment(key=best.key, reason="similarity", topic=best, similarity=best_score)
    return Assignment(key=_new_key(candidate), reason="new", similarity=best_score)


def _new_key(candidate: Candidate) -> str:
    """A key for a topic that does not exist yet, derived from the memory's own words.

    Derived rather than random, so the same memory arriving twice by two paths proposes the
    same key and a key is legible in a log. A memory with no comparable words at all still
    needs somewhere to go, and `untitled` is a topic a person will notice and rename, which
    is the point of choosing a name that looks wrong.
    """
    words = _tokens(candidate.body)[:KEY_WORDS]
    return "-".join(words) if words else "untitled"


def confirm_topic(topic: Topic, *, trust: Trust = Trust.stated) -> Topic:
    """Promote an untrusted topic so that it may enter the index.

    A function rather than a mutable field, so the promotion is a named, greppable act with
    one place to hang an audit hook, instead of an assignment that could happen anywhere.

    The level asked for is resolved before it is judged, so a word this service does not
    recognise is refused here rather than silently demoting the topic it claimed to promote.

    Raises:
        ValueError: `trust` is `untrusted`, or a level this service does not recognise,
            neither of which is a confirmation of anything.
    """
    wanted = _as_trust(trust)
    if wanted is Trust.untrusted:
        message = (
            f"confirming topic {topic.id!r} means giving it a trust other than 'untrusted'; "
            "pass stated, observed or inferred"
        )
        raise ValueError(message)
    return replace(topic, trust=wanted)


def index_line(topic: Topic) -> str:
    """One topic as one line of the index, which is also what the cut has to pay for.

    Defined beside the selection because a budget has to price something concrete, and a
    renderer free to invent its own line is a renderer that can silently spend twice what
    it was allocated. A renderer may present this differently; it must not present more.
    """
    counts = [f"{topic.count} memories"]
    if topic.unread:
        counts.append(f"{topic.unread} new")
    if topic.unconfirmed:
        counts.append(f"{topic.unconfirmed} unconfirmed")
    return f"{topic.title} ({', '.join(counts)}): {topic.summary}"


def _decay(last_seen: datetime | None, reference: datetime | None) -> float:
    """Recency as a half-life from last access, in [0.0, 1.0].

    A topic never accessed scores zero rather than counting as brand new: never used is the
    opposite of just used, and defaulting the other way would let a bulk import outrank
    everything a person actually refers to.

    Age is clamped at zero so that clock skew, or a memory service whose stamp runs a
    little ahead of the hub's, cannot mint a score above one and outrank a genuinely
    current topic by an unbounded amount.
    """
    if last_seen is None or reference is None:
        return 0.0
    age_days = max(0.0, (reference - last_seen).total_seconds() / SECONDS_PER_DAY)
    return math.pow(0.5, age_days / HALF_LIFE_DAYS)


def _minmax(values: Sequence[float]) -> tuple[float, ...]:
    """Each value's position between the set's smallest and largest.

    A dimension every topic agrees on -- all importance 0.5, nobody with unread memories --
    says nothing about which topic to prefer, so it contributes nothing rather than
    contributing its weight to everybody. Either convention leaves the ordering unchanged,
    since a constant is added to every score alike; zero is the one that keeps a printed
    score readable, because then the number shown is the part that did the work.
    """
    low = min(values)
    high = max(values)
    if high <= low:
        return tuple(0.0 for _ in values)
    span = high - low
    return tuple((value - low) / span for value in values)


def _latest(topics: Sequence[Topic]) -> datetime | None:
    """The most recent access in the set, or None when nothing has ever been accessed."""
    seen = [topic.last_seen for topic in topics if topic.last_seen is not None]
    return max(seen) if seen else None


def _order(ranked: Ranked) -> tuple[float, int, str]:
    """The total order: score, then size, then id.

    Size breaks the first tie because a topic holding more memories is the one whose line
    buys the model more. Id breaks the second because it is unique and stable, which is the
    only property a final tie-break needs.
    """
    return (-round(ranked.score, SCORE_PRECISION), -ranked.topic.count, ranked.topic.id)


def rank_topics(topics: Iterable[Topic], *, now: datetime | None = None) -> tuple[Ranked, ...]:
    """Every topic, best first, with the score that placed it.

    `now` is an argument because a clock read inside a ranking function is a ranking that
    cannot be tested. Left out, the most recently accessed topic in the set becomes the
    reference, which is what a caller with no clock to hand means, and it keeps the whole
    computation set-relative exactly as the normalisation already is.
    """
    chosen = tuple(topics)
    if not chosen:
        return ()

    reference = now if now is not None else _latest(chosen)
    recency = _minmax([_decay(topic.last_seen, reference) for topic in chosen])
    importance = _minmax([topic.importance for topic in chosen])
    unread = _minmax([float(topic.unread) for topic in chosen])

    ranked = [
        Ranked(
            topic=topic,
            score=RECENCY_WEIGHT * fresh + IMPORTANCE_WEIGHT * weight + UNREAD_WEIGHT * pending,
            recency=fresh,
            importance=weight,
            unread=pending,
        )
        for topic, fresh, weight, pending in zip(chosen, recency, importance, unread, strict=True)
    ]
    return tuple(sorted(ranked, key=_order))


def select_topics(
    topics: Iterable[Topic],
    *,
    limit: int,
    counter: Counter,
    now: datetime | None = None,
    include_untrusted: bool = False,
) -> Selection:
    """The topics that fit in `limit` tokens, and an honest count of the ones that did not.

    `limit` is a ceiling in tokens, not a number of topics. The index competes for room in
    the pinned band with everything else that has to survive a compaction, and a budget
    measured in topics cannot be reconciled with one measured in tokens -- one long summary
    would quietly spend three short topics' worth. `counter` prices each line; see
    `lucy_api.context.types.Counter` for why counting is injected rather than assumed.

    The selection is always a *prefix* of the ranking. When the next topic does not fit,
    the cut stops there instead of skipping ahead to a shorter one further down, because
    "showing 12 of 47" is only true when the twelve are the top twelve. An index that
    reorders itself by line length is one nobody can explain and nobody should trust.

    `tokens` prices the index lines and nothing else. The notice is written once the cut
    is known -- its wording depends on how many topics were dropped -- so a caller that
    renders it is rendering something this function did not budget for, and the band it
    spends from wants a line's worth of room kept back for it.

    `include_untrusted` is for the one caller that has to show a person what they are being
    asked to confirm. The assembler must never set it: see the module docstring. Setting it
    does not produce a result that looks ordinary, because what comes back says in its own
    notice how much of it is unconfirmed.
    """
    everything = tuple(topics)
    eligible = (
        everything if include_untrusted else tuple(topic for topic in everything if topic.trusted)
    )

    chosen: list[Topic] = []
    spent = 0
    for ranked in rank_topics(eligible, now=now):
        cost = counter.count(index_line(ranked.topic))
        if spent + cost > limit:
            break
        chosen.append(ranked.topic)
        spent += cost

    return Selection(
        topics=tuple(chosen),
        omitted=len(eligible) - len(chosen),
        withheld=len(everything) - len(eligible),
        tokens=spent,
    )


__all__ = [
    "HALF_LIFE_DAYS",
    "IMPORTANCE_WEIGHT",
    "KEY_WORDS",
    "RECENCY_WEIGHT",
    "SCORE_PRECISION",
    "SIMILARITY_THRESHOLD",
    "UNREAD_WEIGHT",
    "Assignment",
    "AssignmentReason",
    "Candidate",
    "FakeTopics",
    "Memory",
    "Ranked",
    "Selection",
    "Topic",
    "TopicSource",
    "UnknownTopicError",
    "assign_topic",
    "confirm_topic",
    "index_line",
    "jaccard",
    "normalise",
    "rank_topics",
    "select_topics",
]
