"""The hub's log, in a file a person on the machine can read and filter.

The application log went to stdout only, which is right for a container and no help to a
person trying to follow one conversation: they had to know how their container runtime keeps
logs, and the lines of every service arrived interleaved. With `LUCY_LOG_FILE` set, the same
JSON lines also go to one file the hub rotates itself.
"""

from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING

import pytest
from conftest import build_settings

from lucy_api.api.app import create_app
from lucy_api.core.config import LogFormat
from lucy_api.core.logging import JsonFormatter, configure

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def restored() -> Iterator[None]:
    """Whatever `configure` installs is taken down again, so no test writes to another's file."""
    root = logging.getLogger()
    level = root.level
    try:
        yield
    finally:
        for handler in tuple(root.handlers):
            if isinstance(handler.formatter, JsonFormatter):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_named_file_gets_the_same_lines_as_stdout(tmp_path: Path) -> None:
    stream = io.StringIO()
    target = tmp_path / "log" / "lucy.jsonl"

    configure(level="INFO", stream=stream, file=str(target))
    logging.getLogger("lucy.test.file").info("hello", extra={"outcome": "ok"})

    [line] = _read(target)
    assert (line["message"], line["outcome"]) == ("hello", "ok")
    assert json.loads(stream.getvalue().splitlines()[0])["message"] == "hello"


def test_the_file_is_rotated_and_only_so_many_are_kept(tmp_path: Path) -> None:
    target = tmp_path / "lucy.jsonl"
    configure(level="INFO", stream=io.StringIO(), file=str(target), max_bytes=400, backups=2)
    logger = logging.getLogger("lucy.test.rotate")

    for index in range(40):
        logger.info("line %d", index)

    kept = sorted(path.name for path in tmp_path.iterdir())
    assert kept == ["lucy.jsonl", "lucy.jsonl.1", "lucy.jsonl.2"]


def test_a_file_that_cannot_be_opened_is_said_and_the_hub_goes_on(tmp_path: Path) -> None:
    stream = io.StringIO()
    occupied = tmp_path / "a-directory"
    occupied.mkdir()

    configure(level="INFO", stream=stream, file=str(occupied))

    said = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert said[0]["message"].startswith("log_file_unavailable")
    assert str(occupied) in said[0]["message"]
    assert sum(isinstance(h.formatter, JsonFormatter) for h in logging.getLogger().handlers) == 1


def test_configuring_again_closes_the_file_it_wrote_before(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    configure(level="INFO", stream=io.StringIO(), file=str(first))
    configure(level="INFO", stream=io.StringIO())

    logging.getLogger("lucy.test.closed").info("after")

    first.unlink()  # a handle still open on Windows would refuse this
    assert not first.exists()


def test_the_app_writes_to_the_file_its_settings_name(tmp_path: Path) -> None:
    target = tmp_path / "hub.jsonl"
    create_app(build_settings(log_format=LogFormat.JSON, log_file=str(target)))

    logging.getLogger("lucy.test.app").warning("from the app")

    assert _read(target)[-1]["message"] == "from the app"
