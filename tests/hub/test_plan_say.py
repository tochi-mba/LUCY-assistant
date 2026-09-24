"""A model can answer without acting, on a provider that enforces the plan schema.

The plan schema goes to every provider as structured output -- Anthropic's
`output_config.format`, OpenAI's `text.format`, chat-completions' `response_format` -- and a
provider that can enforce a schema does. It required `steps`. So a model asked "hi" through
one had no way to answer: it was made to invent a step, and a turn could end only on its
iteration cap. Measured through clyde, whose Claude Code enforces `--json-schema`:
"Hi Lucy! How are you today?" came back `{"steps":[{"id":"ready_to_assist",
"op":"capabilities.list"}]}`, twice in two.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_model_anthropic import message
from test_model_chat import completion
from test_model_openai import response, said

from lucy_api.model.anthropic import reply_from as anthropic_reply
from lucy_api.model.chat import reply_from as chat_reply
from lucy_api.model.openai import reply_from as openai_reply
from lucy_api.model.types import SAY, SAY_DESCRIPTION, Stop
from lucy_api.model.wire import said_and_planned
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.prompt.sections import PromptContext, render_all
from lucy_api.sessions.scope import SessionScope

STEPS = [{"id": "caps", "op": "capabilities.list", "input": {}}]


def _schema() -> dict[str, Any]:
    async def build() -> dict[str, Any]:
        capabilities = Capabilities((HelpPack(),))
        context = capabilities.context_for(
            SessionScope(account_id="acct", profile="personal", session_id="ses")
        )
        return capabilities.plan_schema(await capabilities.probe(context), "ses", context)

    return asyncio.run(build())


def test_the_plan_schema_takes_an_answer_of_words_alone() -> None:
    """The bug, named: `steps` was required, so words alone could not match."""
    schema = _schema()
    assert "steps" not in schema["required"]
    assert schema["properties"][SAY] == {"type": "string", "description": SAY_DESCRIPTION}
    assert schema["properties"]["steps"]["minItems"] == 1, "a plan still has a step"
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello! I'm well.", ("Hello! I'm well.", None)),
        ('{"say": "Hello! I\'m well."}', ("Hello! I'm well.", None)),
        (
            '{"say": "Let me look.", "steps": ' + json.dumps(STEPS) + "}",
            ("Let me look.", {"steps": STEPS}),
        ),
        ('{"steps": ' + json.dumps(STEPS) + "}", ("", {"steps": STEPS})),
        ('{"say": "   "}', ("", None)),
        ('{"say": 7}', ("", None)),
        ('{"answer": "not a plan"}', ("", {"answer": "not a plan"})),
    ],
)
def test_a_reply_is_read_as_words_a_plan_or_both(
    text: str, expected: tuple[str, dict[str, Any] | None]
) -> None:
    assert said_and_planned(text) == expected


def test_a_narrated_plan_keeps_its_say_and_drops_the_sentence_in_front() -> None:
    text = 'Checking now.\n\n{"say": "One moment.", "steps": ' + json.dumps(STEPS) + "}"
    assert said_and_planned(text, narrated=True) == ("One moment.", {"steps": STEPS})
    assert said_and_planned(text) == (text, None), "strict readers never dig into prose"


ANSWER = json.dumps({SAY: "I'm well, thanks for asking."})


def test_anthropic_hands_back_an_answer_in_words_as_the_reply() -> None:
    reply = anthropic_reply(message(content=[{"type": "text", "text": ANSWER}]), want_plan=True)
    assert (reply.text, reply.plan, reply.stop) == (
        "I'm well, thanks for asking.",
        None,
        Stop.end_turn,
    )


def test_openai_hands_back_an_answer_in_words_as_the_reply() -> None:
    reply = openai_reply(response(output=[said(ANSWER)]), want_plan=True)
    assert (reply.text, reply.plan, reply.stop) == (
        "I'm well, thanks for asking.",
        None,
        Stop.end_turn,
    )


def test_a_chat_provider_hands_back_an_answer_in_words_as_the_reply() -> None:
    payload = completion(
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": ANSWER},
                "finish_reason": "stop",
            }
        ]
    )
    reply = chat_reply(payload, provider="clyde", want_plan=True)
    assert (reply.text, reply.plan, reply.stop) == (
        "I'm well, thanks for asking.",
        None,
        Stop.end_turn,
    )


def test_words_beside_steps_come_back_as_the_text_before_the_plan() -> None:
    text = json.dumps({SAY: "Looking at what I can do.", "steps": STEPS})
    reply = anthropic_reply(message(content=[{"type": "text", "text": text}]), want_plan=True)
    assert (reply.text, reply.plan, reply.stop) == (
        "Looking at what I can do.",
        {"steps": STEPS},
        Stop.tool_use,
    )


def test_a_round_with_no_plan_asked_for_is_prose_whatever_it_looks_like() -> None:
    reply = anthropic_reply(message(content=[{"type": "text", "text": ANSWER}]), want_plan=False)
    assert (reply.text, reply.plan) == (ANSWER, None)


def test_words_beside_steps_say_what_is_about_to_happen_not_that_it_happened() -> None:
    """Read in a captured turn: "Noted -- Friday afternoons for your weekly review." reached the
    person beside a write that then stopped for approval. Nothing had been kept, and the words
    would have stood if the answer was no. The schema called them "said while they run", and
    the memory prompt said to keep a fact "and say so in a clause" in the same breath.
    """
    assert "shown only once they have" in SAY_DESCRIPTION
    assert "never that it is done" in SAY_DESCRIPTION
    prompt = " ".join(" ".join(section.body for section in render_all(PromptContext())).split())
    assert "once it is kept say so in a clause" in prompt
    assert "nothing you wrote beside it is shown" in prompt
