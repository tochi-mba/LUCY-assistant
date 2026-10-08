"""The Claude Code bridge: Lucy delegates a task, a real Claude Code session runs it.

The hub runs in Docker and cannot start host processes; Claude Code lives on the host with
the person's own login. This package is the piece between them: a small host-run service
(127.0.0.1:8012) that wraps the ``claude`` CLI as subprocesses, keeps one durable task row
and one JSONL transcript per delegation, and answers the hub's coder capability.

It is deployment glue for this machine, like the hub itself (ADR-0009) and clyde: in the
meta repository, never a sibling repo. ADR-0017 holds the design; the three switches --
operator (the hub's ``coder_api_base_url``), person (``lucy.claude_code_*`` settings), and
per-task (an approval card every time) -- live on the hub side, not here. This service
trusts exactly one thing: a keyring token minted for the ``coder-api`` audience.
"""
