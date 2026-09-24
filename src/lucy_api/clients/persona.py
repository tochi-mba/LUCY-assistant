"""Lessons: how this person wants the assistant to work, kept where they come back every turn.

A lesson is a Persona-api note with `kind: lesson`, written by the assistant and pinned. Pinned
persona notes already reach every conversation through the persona feed, as standing claims
with their provenance, so writing one here is all it takes for Lucy to "read it back in every
later conversation". Nothing else stores or loads them.

Persona-api's forget is a tombstone, not a deletion: a lesson unlearned by mistake can still be
found by the person. Its caps are real -- a persona holds a bounded number of notes and of
pinned ones -- and an at-the-cap write is a `RateLimitedError` whose detail names the cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.errors import AbsentError, RateLimitedError
from lucy_api.clients.transport import Sibling, flag, number, segment, text

if TYPE_CHECKING:
    from lucy_api.packs.context import Http

SERVICE = "persona"
AUDIENCE = "persona"
"""What persona-api pins its tokens against, exactly. See `live_feeds.PERSONA_AUDIENCE`."""

LESSON = "lesson"
ASSISTANT = "assistant"
"""Who asserts a lesson. Persona-api reads `assistant` as inferred, never as the person."""


@dataclass(frozen=True, slots=True)
class Lesson:
    """One lesson, as a tool result may carry it."""

    id: str
    body: str
    pinned: bool = True
    revision: int = 1


class PersonaClient(Protocol):
    """The lessons surface, as the notes pack calls it."""

    async def learn(self, body: str, *, profile: str) -> Lesson: ...

    async def revise(self, lesson_id: str, body: str, *, profile: str) -> Lesson: ...

    async def unlearn(self, lesson_id: str, *, profile: str) -> None: ...


class HttpPersonaClient:
    """Persona-api's note routes, for lessons only."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def learn(self, body: str, *, profile: str) -> Lesson:
        """Write a pinned lesson, so the persona feed carries it into every conversation."""
        payload = await self._api.send(
            "POST",
            _notes(profile),
            body={"body": body, "kind": LESSON, "source": ASSISTANT, "pinned": True},
            profile=profile,
        )
        return _lesson(payload)

    async def revise(self, lesson_id: str, body: str, *, profile: str) -> Lesson:
        """Replace a lesson's words. Persona-api keeps the revision count, not the old text."""
        payload = await self._api.send(
            "PATCH",
            f"{_notes(profile)}/{segment(lesson_id)}",
            body={"body": body, "source": ASSISTANT},
            profile=profile,
        )
        return _lesson(payload)

    async def unlearn(self, lesson_id: str, *, profile: str) -> None:
        """Tombstone a lesson. It stops coming back; the person can still find it."""
        await self._api.send("DELETE", f"{_notes(profile)}/{segment(lesson_id)}", profile=profile)


class FakePersonaClient:
    """Lessons in a dictionary, so the notes pack's tests need no Persona-api.

    It answers as the service does where it matters to a caller: an unknown id is a 404, and
    a write past `max_pinned` is the 429 that names the cap.
    """

    def __init__(self, *, max_pinned: int = 10) -> None:
        self.lessons: dict[str, Lesson] = {}
        self.unlearned: list[str] = []
        self.max_pinned = max_pinned

    async def learn(self, body: str, *, profile: str) -> Lesson:
        del profile
        if len(self.lessons) >= self.max_pinned:
            detail = f"at most {self.max_pinned} pinned notes per persona"
            raise RateLimitedError(SERVICE, 429, detail)
        lesson = Lesson(id=f"note_{len(self.lessons) + len(self.unlearned) + 1}", body=body)
        self.lessons[lesson.id] = lesson
        return lesson

    async def revise(self, lesson_id: str, body: str, *, profile: str) -> Lesson:
        del profile
        current = self._held(lesson_id)
        revised = Lesson(id=lesson_id, body=body, revision=current.revision + 1)
        self.lessons[lesson_id] = revised
        return revised

    async def unlearn(self, lesson_id: str, *, profile: str) -> None:
        del profile
        self._held(lesson_id)
        del self.lessons[lesson_id]
        self.unlearned.append(lesson_id)

    def _held(self, lesson_id: str) -> Lesson:
        if lesson_id not in self.lessons:
            raise AbsentError(SERVICE, 404, "Note not found.")
        return self.lessons[lesson_id]


def as_dict(lesson: Lesson) -> dict[str, Any]:
    """A lesson as a tool result: the id to revise or unlearn it by, and its words."""
    return {"lesson_id": lesson.id, "lesson": lesson.body, "revision": lesson.revision}


def _notes(profile: str) -> str:
    return f"/v1/personas/{segment(profile)}/notes"


def _lesson(payload: Any) -> Lesson:
    return Lesson(
        id=text(payload, "note_id"),
        body=text(payload, "body"),
        pinned=flag(payload, "pinned", default=True),
        revision=number(payload, "revision", 1),
    )


__all__ = [
    "ASSISTANT",
    "AUDIENCE",
    "LESSON",
    "SERVICE",
    "FakePersonaClient",
    "HttpPersonaClient",
    "Lesson",
    "PersonaClient",
    "as_dict",
]
