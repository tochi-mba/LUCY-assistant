"""The typed brief a parent hands a helper, and the cap on what comes back.

A one-line objective throws away the multi-turn reasoning that produced the question. The
brief is a struct so every field a child needs has a name, and so a missing field is a
missing field rather than a sentence the child has to infer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from lucy_api.work.registry import rough_tokens

RESULT_TOKEN_CAP = 2000
"""How large a helper's return may be.

A child dumping its transcript on the parent is worse than inlining the work. Two thousand
tokens is a summary plus references; the rest stays behind the child's own items.
"""

RESULT_CHAR_CAP = RESULT_TOKEN_CAP * 4
"""Four characters to a token, same rule as the work registry. Close enough to gate on."""

CONTINUABLE = "stopped before it finished; agents.reopen continues it"
"""The front of the notice for a helper that can be picked up where it stopped.

First, because the notice is clipped from the end: a long reason loses its tail, not the
one thing the model can do about it.
"""

STOPPED = "stopped before it finished"
"""The front of the notice for a helper that cannot usefully be continued as it is."""

RESTARTED = "the hub restarted while it was running"
"""Why a helper a previous process was running has stopped."""


@dataclass(frozen=True, slots=True)
class Delegation:
    """What the child is for, written once, read by the child, the live block and the audit."""

    objective: str
    role: str = "helper"
    output_format: str = "a short summary with references, never a raw transcript"
    guidance: str = ""
    boundaries: str = ""
    constraints: str = ""
    max_iterations: int = 8
    resume_from: str = ""
    return_schema: str = ""


def capped_summary(text: str) -> tuple[str, int, str]:
    """Clip a helper's return to the contract, and say so when anything was left out."""
    tokens = rough_tokens(text)
    if tokens <= RESULT_TOKEN_CAP:
        return text, tokens, ""
    clipped = text[: RESULT_CHAR_CAP - 1].rstrip() + "…"
    notice = f"showing {RESULT_TOKEN_CAP} of {tokens} tokens"
    return clipped, RESULT_TOKEN_CAP, notice


def declared_return(text: str, schema: str) -> tuple[object | None, str]:
    """Parse a helper's answer as JSON when the caller declared a schema.

    Full JSON Schema validation is not done here: the declaration is a contract the child
    is told about in its brief, and the parent gets either an object or an honest miss.
    Inventing a schema validator would fail closed on drafts we do not pin.
    """
    if not schema.strip():
        return None, ""
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError:
        return None, "the helper's return was not valid JSON for the declared schema"
    if not isinstance(data, dict):
        return None, "the helper's return must be a JSON object"
    return data, ""


__all__ = [
    "CONTINUABLE",
    "RESTARTED",
    "RESULT_CHAR_CAP",
    "RESULT_TOKEN_CAP",
    "STOPPED",
    "Delegation",
    "capped_summary",
    "declared_return",
]
