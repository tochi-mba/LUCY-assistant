"""A failed step says what it means for the person and what to do next, never `x answered 409`.

The bug, named: `clients.errors` says its messages are for a log or a pack, never a prompt, and
that a pack translates them. Only repos did. Every other pack let `memory answered 409: ...`
through -- a service name and a status code the house rules keep out of the prompt, and no next
step -- and weftai's timeout advised raising a limit the model may not touch.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.clients.errors import (
    AbsentError,
    ConflictError,
    DownstreamError,
    ForbiddenError,
    NotConnectedError,
    PreconditionError,
    RateLimitedError,
    ReauthenticationError,
    RejectedError,
    UnavailableError,
)
from lucy_api.packs.http import DownstreamUnavailableError
from lucy_api.packs.registry import build_registry
from lucy_api.packs.service import as_loop_result
from lucy_api.packs.spoken import RAN_OUT, StepRefusedError, said, spoken, timed_out


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotConnectedError("memory", 403), "notes is not connected for this person"),
        (
            RateLimitedError("memory", 429, retry_after=7),
            "notes is limiting requests. Try again in 7s.",
        ),
        (RateLimitedError("memory", 429), "notes is limiting requests. Try again later."),
        (ForbiddenError("memory", 403), "notes does not allow this for this person right now"),
        (ReauthenticationError("memory", 401), "notes does not allow this"),
        (AbsentError("memory", 404, "No note mem_9."), "No note mem_9. Use an id from an earlier"),
        (AbsentError("memory", 404), "Nothing has that id. Use an id"),
        (ConflictError("memory", 409, "At 200 notes"), "At 200 notes. That is a limit"),
        (PreconditionError("memory", 412), "It changed. It changed since you read it"),
        (RejectedError("memory", 422, "Too long"), "Too long. Change what you sent"),
        (UnavailableError("memory", 503), "notes could not be reached just now. Retry once"),
        (DownstreamError("memory", 500), "notes could not be reached just now"),
    ],
)
def test_every_refusal_is_said_in_product_words_with_a_next_step(
    error: DownstreamError, expected: str
) -> None:
    text = said(error, "notes")
    assert text.startswith(expected) or expected in text
    assert "memory" not in text, "never the service's name"
    assert "answered" not in text, "never a status line"


def _operation(run: Any) -> Any:
    return define_operation(
        {
            "name": "notes.search",
            "description": "Search.",
            "input": object_schema({}),
            "output": value(object_schema({"ok": string_schema()})),
            "effects": "read",
            "run": run,
        }
    )


async def test_a_wrapped_operation_says_its_refusal_and_keeps_the_original_for_the_log() -> None:
    async def refused(_run: object) -> None:
        raise ConflictError("memory", 409, "At 200 notes")

    with pytest.raises(StepRefusedError) as caught:
        await spoken(_operation(refused)).run(SimpleNamespace())
    assert str(caught.value).startswith("At 200 notes. That is a limit")
    assert isinstance(caught.value.__cause__, ConflictError)


async def test_a_transport_failure_is_said_as_unreachable() -> None:
    async def down(_run: object) -> None:
        raise DownstreamUnavailableError("memory answered 503", audience="memory-api", status=503)

    with pytest.raises(StepRefusedError, match="notes could not be reached"):
        await spoken(_operation(down)).run(SimpleNamespace())


async def test_an_answer_and_any_other_failure_pass_through_untouched() -> None:
    def plain(_run: object) -> dict[str, str]:
        return {"ok": "yes"}

    def broken(_run: object) -> None:
        raise ValueError("a bug of ours")

    assert await spoken(_operation(plain)).run(SimpleNamespace()) == {"ok": "yes"}
    with pytest.raises(ValueError, match="a bug of ours"):
        await spoken(_operation(broken)).run(SimpleNamespace())


async def test_the_registry_holds_the_wrapped_operations() -> None:
    async def refused(_run: object) -> None:
        raise AbsentError("memory", 404)

    registry = build_registry([_operation(refused)], with_standard=False)
    [operation] = [registry.get("notes.search")]
    with pytest.raises(StepRefusedError, match="Nothing has that id"):
        await operation.run(SimpleNamespace())


def test_a_timeout_is_told_what_it_can_do_and_never_to_raise_a_limit() -> None:
    weftai = "Step 'search_py' timed out after 30000ms. Narrow the query or raise the step timeout."
    assert timed_out(weftai) == RAN_OUT.format(id="search_py", seconds=30)
    assert "raise" not in timed_out(weftai)
    assert timed_out("something else") == "something else"
    shaped = as_loop_result(
        {"steps": [{"id": "a", "error": weftai}, {"id": "b", "error": None}, "odd"]}
    )
    assert shaped["steps"][0]["error"].startswith("Step 'search_py' ran out of time after 30s")
    assert shaped["steps"][1] == {"id": "b", "error": None}
    assert shaped["steps"][2] == "odd"
