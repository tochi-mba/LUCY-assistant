"""Which topic a new memory joins, decided rather than measured by word overlap.

`memory/topics.py` files a keyless memory by Jaccard overlap against each existing topic, and
its own docstring pre-authorises replacing the measure behind the same signature. That is what
this does, under three constraints that are the whole reason it is safe:

**A key is never touched.** A candidate carrying a key settles the question on its own, and
folding one key into another is a decision about the taxonomy rather than about this memory.
:func:`decide_topic` is only ever consulted on the keyless path.

**It can only choose among topics the overlap pass could also have chosen.** The answer is one
of the existing topic keys or an abstention, so the worst outcome is a memory filed under a
topic it resembles less than another one -- never a topic that does not exist, and never a
merge across keys.

**Abstaining means a new topic**, which is also what the overlap pass does when nothing clears
its threshold. An out-of-scope candidate has somewhere correct to go, which matters more here
than anywhere else: a decision model with no "none of these" option does not decline, it picks
the least wrong option and reports an ordinary confidence for it, and a genuinely new subject
is exactly the input that has no right answer in the set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from weftai.decisions import Answers, Batch, ChoiceQuestion, Gate, choice

if TYPE_CHECKING:
    from collections.abc import Sequence

ABSTAIN = "none of these"
"""What the model says when the candidate belongs to none of the existing topics. The same
string the builder supplies by default, named here because this module compares against it."""

THRESHOLD = 0.5
"""Below this, the word-overlap pass decides instead.

Middling rather than high, because both outcomes are recoverable and roughly equally cheap: a
memory in a slightly wrong topic is visible in the index and re-filable, and a topic that
should have merged can be merged later. It is a calibrated figure only once there is a golden
set to calibrate it against; until then it is a starting point and the event stream is how
anyone would know it was wrong.
"""

MIN_TOPICS = 2
"""One topic is not a choice, and zero is not a question."""

MAX_TOPICS = 40
"""Beyond this the question stops being a classification and becomes a search, and a wide
option set is where a decision model's calibration is worst. Past it, overlap decides."""


def topic_question(body: str, keys: Sequence[str]) -> ChoiceQuestion:
    """The one question: which of these topics does this note belong to?

    The topic keys are the options and the note is the state, so the question itself carries
    no content -- which is what keeps it one atomic judgement rather than a summary.
    """
    _ = body
    return choice(
        "topic",
        "Which existing topic does this note belong to?",
        list(keys),
        criteria=(
            "Choose a topic only when the note is about the same subject. Choose none of "
            "these when it is about something the listed topics do not cover."
        ),
    )


def topic_batch(body: str, keys: Sequence[str]) -> Batch | None:
    """The batch to ask, or `None` when there is nothing worth asking.

    Fewer than two topics is not a choice, and more than :data:`MAX_TOPICS` is a search.
    Returning `None` rather than an empty batch keeps the "do not ask" decision here, where
    the reasons are, instead of at the call site.
    """
    if not MIN_TOPICS <= len(keys) <= MAX_TOPICS:
        return None
    return Batch().add(topic_question(body, keys))


def decided_key(answers: Answers, keys: Sequence[str], *, threshold: float = THRESHOLD) -> str:
    """The chosen topic key, or `""` for "the overlap pass decides".

    An abstention and an unanswered question both come back as `""`, and they mean different
    things to a reader but the same thing to a caller: this did not pick an existing topic.
    The caller's fallback covers both, which is why they are not distinguished here.
    """
    gate: Gate[str] = Gate(threshold, fail_open="")
    chosen = gate.decide(answers, "topic", answers.choice("topic"))
    if chosen in (ABSTAIN, ""):
        return ""
    # A key the model invented is not a key. The option set was the existing topics, so
    # anything outside it is a malformed answer and the overlap pass decides.
    return chosen if chosen in keys else ""


__all__ = [
    "ABSTAIN",
    "MAX_TOPICS",
    "MIN_TOPICS",
    "THRESHOLD",
    "decided_key",
    "topic_batch",
    "topic_question",
]
