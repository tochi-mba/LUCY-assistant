"""examples/hello-api is the template for a new service, so it must pass the family standard."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import parity  # noqa: E402


def test_the_example_passes_every_parity_check() -> None:
    report = parity.evaluate(ROOT / "examples", ["hello-api"])[0]
    assert report.present
    failed = {
        item.check.id: item.result.detail
        for item in report.outcomes
        if item.result.status == parity.FAIL
    }
    assert failed == {}
    assert {item.check.id for item in report.outcomes} == {check.id for check in parity.CHECKS}
