"""What a change cost or saved: tokens, rounds and time, set against a previous run.

Pass rates say whether Lucy still does the job. These say how much it took: the tokens
sent and received, how many model rounds a turn needed, how long the person waited, and
how big the fixed prompt every request carries is. An optimisation that holds every check
and reads nothing here is a guess; one that moves these numbers is measured.

Compared over the scenarios both runs held, per run (so `--repeat` reads the same as one
run), from the figures every report already records. A run that errored before a turn was
sent measures nothing and is left out; a failed run still spent what it spent.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from lucy_api.evals.results import ERROR, SKIPPED

METRICS = ("input_tokens", "output_tokens", "cache_read_tokens", "rounds", "seconds", "turns")
"""Lower is better for every one of them."""

LABELS = {
    "input_tokens": "input tokens",
    "output_tokens": "output tokens",
    "cache_read_tokens": "cached tokens",
    "rounds": "model rounds",
    "seconds": "seconds",
    "turns": "turns",
}

Key = tuple[str, str]


def measures(run: dict[str, Any]) -> dict[str, float]:
    """One run's spend, from its turns."""
    turns = [turn for turn in run.get("turns") or () if isinstance(turn, dict)]
    return {
        "input_tokens": float(sum(_number(turn, "input_tokens") for turn in turns)),
        "output_tokens": float(sum(_number(turn, "output_tokens") for turn in turns)),
        "cache_read_tokens": float(sum(_number(turn, "cache_read_tokens") for turn in turns)),
        "rounds": float(sum(_number(turn, "iterations") for turn in turns)),
        "seconds": float(_number(run, "seconds")),
        "turns": float(len(turns)),
    }


def efficiency(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    """Per model and per scenario, before and after, over the scenarios both runs held."""
    before, after = _means(previous), _means(current)
    shared = sorted(before.keys() & after.keys())
    scenarios = [
        {
            "model": model,
            "scenario": scenario,
            **{
                metric: delta(before[(model, scenario)][metric], after[(model, scenario)][metric])
                for metric in METRICS
            },
        }
        for model, scenario in shared
    ]
    models: dict[str, Any] = {}
    for model in sorted({model for model, _ in shared}):
        keys = [key for key in shared if key[0] == model]
        models[model] = {
            "scenarios": len(keys),
            **{
                metric: delta(
                    sum(before[key][metric] for key in keys),
                    sum(after[key][metric] for key in keys),
                )
                for metric in METRICS
            },
        }
    return {"models": models, "scenarios": scenarios}


def prompt_change(previous: object, current: object) -> dict[str, Any] | None:
    """The fixed prompt's size before and after, and every section that moved."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return None
    old, new = _sections(previous), _sections(current)
    moved = [
        {"section": name, "before": old.get(name, 0), "after": new.get(name, 0)}
        for name in sorted(old.keys() | new.keys())
        if old.get(name, 0) != new.get(name, 0)
    ]
    return {
        "total": delta(_number(previous, "total"), _number(current, "total")),
        "sections": moved,
    }


def delta(before: float, after: float) -> dict[str, Any]:
    """`change` is a percentage of `before`; there is none when `before` was nothing."""
    change = None if before == 0 else round((after - before) / before * 100, 1)
    return {"before": round(before, 3), "after": round(after, 3), "change": change}


def summary_lines(comparison: dict[str, Any]) -> list[str]:
    """A few lines a person reads at the end of a run."""
    lines: list[str] = []
    prompt = comparison.get("prompt")
    if isinstance(prompt, dict):
        lines.append(f"  fixed prompt tokens {_moved(prompt['total'])}")
    for model, row in (comparison.get("efficiency") or {}).get("models", {}).items():
        spent = ", ".join(f"{LABELS[metric]} {_moved(row[metric])}" for metric in METRICS)
        lines.append(f"  {model} over {row['scenarios']} shared scenario(s): {spent}")
    return lines


def _moved(row: dict[str, Any]) -> str:
    before, after, change = row["before"], row["after"], row["change"]
    shown = f"{_figure(before)} -> {_figure(after)}"
    return shown if change is None else f"{shown} ({change:+.1f}%)"


def _figure(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _means(runs: list[dict[str, Any]]) -> dict[Key, dict[str, float]]:
    grouped: dict[Key, list[dict[str, float]]] = defaultdict(list)
    for run in runs:
        if run.get("outcome") in {SKIPPED, ERROR}:
            continue
        grouped[(str(run.get("model")), str(run.get("scenario")))].append(measures(run))
    return {
        key: {metric: sum(row[metric] for row in rows) / len(rows) for metric in METRICS}
        for key, rows in grouped.items()
    }


def _sections(prompt: dict[str, Any]) -> dict[str, int]:
    sections = prompt.get("sections")
    if isinstance(sections, dict):
        return {str(name): int(value) for name, value in sections.items() if isinstance(value, int)}
    return {}


def _number(source: dict[str, Any], key: str) -> float:
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


__all__ = ["LABELS", "METRICS", "delta", "efficiency", "measures", "prompt_change", "summary_lines"]
