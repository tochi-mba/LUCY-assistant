"""A stand-in for the ``claude`` CLI: argv in, canned stream-json out.

The tests never run the real CLI -- a test suite that spent the person's subscription would
be a bug of its own. This script speaks exactly the shapes the 2.1.280 CLI was observed to
speak (see ``lucy_coder.runner``), and a ``FAKE_CLAUDE`` environment variable chooses the
script it plays:

- ``answers`` (default): init, a tool use, a text block, a success result.
- ``budget``: a result record whose subtype is ``error_max_budget_usd``.
- ``limit``: the CLI's own session-limit sentence in an error result.
- ``signed-out``: the login complaint, then exit without a result record.
- ``hangs``: the init record, then sleep far past any test timeout.
- ``garbled``: a line that is not JSON between ordinary ones.

It also writes ``argv.json`` into its working directory, so a test can assert exactly how
the runner invoked it -- flags, session id, cwd -- without parsing a process table.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def line(payload: object) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    script = os.environ.get("FAKE_CLAUDE", "answers")
    argv = sys.argv[1:]
    if "--version" in argv:
        # The doctor's probe runs wherever the service runs; recording it would litter.
        print("2.1.280 (Fake Claude)")
        sys.exit(0)
    Path("argv.json").write_text(
        json.dumps({"argv": argv, "cwd": str(Path.cwd())}), encoding="utf-8"
    )
    session = argv[argv.index("--session-id") + 1] if "--session-id" in argv else ""
    session = argv[argv.index("--resume") + 1] if "--resume" in argv else session
    if script == "mute":
        sys.exit(3)
    line({"type": "system", "subtype": "init", "session_id": session, "model": "fake"})
    if script == "hangs":
        time.sleep(600)
    if script == "garbled":
        sys.stdout.write("warning: not json at all\n")
        sys.stdout.write("\n")
        line({"type": "assistant", "message": {"content": "not a list"}, "session_id": session})
        sys.stdout.flush()
    if script == "signed-out":
        sys.stdout.write("Not logged in. Run /login to sign in.\n")
        sys.stdout.flush()
        sys.exit(1)
    if script in {"answers", "garbled"}:
        line(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Write"}]},
                "session_id": session,
            }
        )
        line(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "writing the file now"}]},
                "session_id": session,
            }
        )
        line(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0.021,
                "result": "done: wrote hello.txt",
                "session_id": session,
            }
        )
        return
    if script == "budget":
        line(
            {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "num_turns": 1,
                "total_cost_usd": 0.22,
                "result": None,
                "session_id": session,
            }
        )
        return
    if script == "denied":
        # What `ask` mode does headless: the write is refused, not prompted, and the result
        # says so -- the shape the 2.1.280 CLI was observed to return.
        line(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0.01,
                "result": "The Write tool needs permission to create denied.txt.",
                "permission_denials": [
                    {
                        "tool_name": "Write",
                        "tool_use_id": "toolu_1",
                        "tool_input": {"file_path": "denied.txt", "content": "hi" * 400},
                    },
                    "not a denial",
                ],
                "session_id": session,
            }
        )
        return
    if script == "signed-out-result":
        line(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 0,
                "total_cost_usd": 0.0,
                "result": "authentication_error: please run /login",
                "session_id": session,
            }
        )
        return
    if script == "limit":
        line(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 1,
                "total_cost_usd": 0.0,
                "result": "You've hit your session limit - resets 1am (Europe/London)",
                "session_id": session,
            }
        )
        return


if __name__ == "__main__":
    main()
