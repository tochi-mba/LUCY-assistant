"""Application logs carry the work-in-flight fields and never a secret or a payload."""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from conftest import build_settings

from lucy_api.api.app import create_app
from lucy_api.core.config import LogFormat
from lucy_api.core.logging import (
    MANDATORY_FIELDS,
    REDACTED,
    JsonFormatter,
    allow_message_content,
    bind,
    configure,
    current_context,
    operation,
    scrub,
    shape,
)
from lucy_api.core.request_id import bind_request_id


def test_a_jwt_and_a_bearer_header_are_scrubbed() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature"
    assert REDACTED in scrub(f"Authorization: Bearer {token}")
    assert token not in scrub(f"Authorization: Bearer {token}")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in scrub("key sk-abcdefghijklmnopqrstuvwxyz")
    assert REDACTED in scrub("ghp_abcdefghijklmnopqrstuvwxyz")
    assert REDACTED in scrub("xoxb-1234567890-abcdefghij")


def test_shape_names_the_kind_without_the_value() -> None:
    assert shape("hello") == {"chars": 5, "sha256": shape("hello")["sha256"]}
    assert shape({"a": 1, "b": 2}) == {"keys": 2}
    assert shape([1, 2, 3]) == {"items": 3}
    assert shape((1, 2)) == {"items": 2}
    assert shape(7) == {"type": "int"}


def test_bind_rejects_a_field_the_line_does_not_carry() -> None:
    with (
        pytest.raises(ValueError, match="cannot bind secret"),
        bind(secret="nope"),  # type: ignore[arg-type]
    ):
        pass


def test_bind_merges_so_a_child_keeps_the_session() -> None:
    with bind(session_id="ses_1"), bind(agent_id="agt_1"):
        bound = current_context()
    assert bound.session_id == "ses_1"
    assert bound.agent_id == "agt_1"
    assert current_context().session_id is None


def test_a_log_line_has_every_mandatory_field_and_redacts_by_name() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "lucy",
        logging.INFO,
        __file__,
        1,
        "hello eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature",
        (),
        None,
    )
    record.access_token = "secret-value"
    record.payload = {"body": "do not log"}
    record.content = "the person's sentence"
    record.count = 4
    record.note = "plain eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature"
    with bind_request_id("req-1"), bind(session_id="ses_1"):
        line = json.loads(formatter.format(record))
    assert all(field in line for field in MANDATORY_FIELDS)
    assert line["request_id"] == "req-1"
    assert line["session_id"] == "ses_1"
    assert line["access_token"] == REDACTED
    assert line["payload"] == {"keys": 1}
    assert line["content"] == shape("the person's sentence")
    assert line["count"] == 4
    assert REDACTED in line["note"]
    assert "secret-value" not in json.dumps(line)
    assert "the person's sentence" not in json.dumps(line)
    assert "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature" not in line["message"]


def test_message_content_is_opt_in_and_exceptions_carry_a_type_not_a_message() -> None:
    formatter = JsonFormatter(log_message_content=True)
    try:
        raise ValueError("the token was abc.def.ghi")
    except ValueError:
        record = logging.LogRecord("lucy", logging.ERROR, __file__, 1, "failed", (), None)
        record.exc_info = sys.exc_info()
        record.content = "keep this"
        line = json.loads(formatter.format(record))
    assert line["content"] == "keep this"
    assert line["error"]["type"] == "ValueError"
    assert "abc.def.ghi" not in json.dumps(line)


def test_a_turn_may_unlock_message_content_without_changing_the_process_formatter() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("lucy", logging.INFO, __file__, 1, "hello", (), None)
    record.content = "the person's sentence"
    with allow_message_content(True):
        unlocked = json.loads(formatter.format(record))
    locked = json.loads(formatter.format(record))
    assert unlocked["content"] == "the person's sentence"
    assert locked["content"] == shape("the person's sentence")


def test_json_log_format_installs_the_structured_handler() -> None:
    root = logging.getLogger()
    try:
        create_app(build_settings(log_format=LogFormat.JSON))
        installed = [
            handler for handler in root.handlers if isinstance(handler.formatter, JsonFormatter)
        ]
        assert installed
    finally:
        for handler in tuple(root.handlers):
            if isinstance(handler.formatter, JsonFormatter):
                root.removeHandler(handler)


def test_configure_replaces_its_own_handler_and_operation_times_both_endings() -> None:
    stream = io.StringIO()
    first = configure(level="INFO", stream=stream)
    configure(level="INFO", stream=stream)
    logger = logging.getLogger("lucy.test.logging")
    with operation(logger, "probe", capability="help"):
        pass
    try:
        with operation(logger, "probe"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    text = stream.getvalue()
    lines = [json.loads(row) for row in text.splitlines() if row.strip()]
    assert any(row.get("outcome") == "ok" for row in lines)
    assert any(
        row.get("outcome") == "error" and row.get("error_type") == "RuntimeError" for row in lines
    )
    logging.getLogger().removeHandler(first)
    for handler in tuple(logging.getLogger().handlers):
        if isinstance(handler.formatter, JsonFormatter):
            logging.getLogger().removeHandler(handler)
