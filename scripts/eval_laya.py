"""Opt-in live calibration smoke test, separate from the offline test gate.

uv run python scripts/eval_laya.py --url http://127.0.0.1:8010
No user data is used. Output contains aggregate counts and timings, never credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time

import httpx
from weftai.decisions import noul
from weftai.providers.laya import LayaDecider

CASES = (
    ("Play some jazz music", "Does the request need music playback?", True),
    ("Explain prime numbers", "Does the request need music playback?", False),
    ("Pon música de jazz", "Does the request need music playback?", True),
    ("What is tomorrow's weather?", "Does this request need up-to-date information?", True),
    ("Write a poem about rain", "Does this request need up-to-date information?", False),
    (
        "Request: dinner ideas. Memory: the person is vegetarian.",
        "Is the remembered fact relevant to the request?",
        True,
    ),
    (
        "Request: fix a Python syntax error. Memory: the person likes jazz.",
        "Is the remembered fact relevant to the request?",
        False,
    ),
    (
        "Attempt 1: connection refused. Attempt 2: unable to connect to the same host.",
        "Do the attempts fail because of the same underlying obstacle?",
        True,
    ),
    (
        "Attempt 1: file missing. Attempt 2: file opened successfully.",
        "Do the attempts fail because of the same underlying obstacle?",
        False,
    ),
)


async def evaluate(decider):
    elapsed = []
    answered = correct = accepted = accepted_correct = 0
    for state, prompt, expected in CASES:
        started = time.perf_counter()
        answers = await decider.decide(state, [noul("judgment", prompt)])
        elapsed.append((time.perf_counter() - started) * 1000)
        answer = answers.get("judgment")
        if answer is None:
            continue
        answered += 1
        correct += answer.value == expected
        if answer.probability >= 0.85:
            accepted += 1
            accepted_correct += answer.value == expected
    return {
        "cases": len(CASES),
        "answered": answered,
        "correct": correct,
        "accepted_at_085": accepted,
        "accepted_correct": accepted_correct,
        "fallbacks": len(CASES) - accepted,
        "latency_ms_median": round(statistics.median(elapsed), 2),
        "latency_ms_max": round(max(elapsed), 2),
        "note": (
            "Small smoke corpus, not a quality benchmark. "
            "Validate on representative traffic in shadow mode."
        ),
    }


async def run(url, model, timeout_ms):
    async with httpx.AsyncClient() as client:
        decider = LayaDecider(
            client,
            url,
            api_key=os.environ.get("LAYA_API_KEY", ""),
            model=model,
            timeout_ms=timeout_ms,
        )
        print(json.dumps(await evaluate(decider), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.model, args.timeout_ms))


if __name__ == "__main__":
    main()
