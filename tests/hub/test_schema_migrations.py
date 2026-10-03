"""The schema an existing database is upgraded to is the schema a new one is created with.

`SCHEMA` creates every table as it is today; `ADDED_COLUMNS` is how a database created
earlier catches up. A column that reaches the first and not the second works on every fresh
install and every test, and fails on the one database that matters: the running hub's.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from lucy_api.sessions.schema import ADDED_COLUMNS, SCHEMA
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker

FIRST_COLUMNS: dict[str, frozenset[str]] = {
    "agent_mail": frozenset(
        {
            "agent_id",
            "body",
            "created_at",
            "delivered_at",
            "direction",
            "hops",
            "id",
            "sender",
        }
    ),
    "agents": frozenset(
        {
            "budget_json",
            "cost_micros",
            "created_at",
            "delegation_json",
            "depth",
            "finished_at",
            "id",
            "input_tokens",
            "interrupted_reason",
            "objective",
            "output_tokens",
            "parent_agent_id",
            "progress",
            "result_json",
            "role",
            "session_id",
            "started_at",
            "status",
            "summary_tokens",
            "tools_json",
            "workspace_rel",
        }
    ),
    "approvals": frozenset(
        {
            "agent_id",
            "decided_at",
            "decided_by",
            "description",
            "id",
            "input_json",
            "instruction",
            "lifetime",
            "operation",
            "policy",
            "requested_at",
            "session_id",
            "status",
            "turn_id",
        }
    ),
    "artifacts": frozenset(
        {
            "bytes",
            "created_at",
            "id",
            "mime_type",
            "path",
            "produced_by",
            "session_id",
        }
    ),
    "audit": frozenset(
        {
            "account_id",
            "action",
            "agent_id",
            "at",
            "detail_json",
            "sequence",
            "session_id",
            "turn_id",
        }
    ),
    "compactions": frozenset(
        {
            "active",
            "covers_from",
            "covers_to",
            "created_at",
            "id",
            "model",
            "prompt_version",
            "seq",
            "session_id",
            "summary",
            "trigger_tokens",
        }
    ),
    "device_codes": frozenset(
        {
            "account_id",
            "created_at",
            "device_code",
            "expires_at",
            "interval",
            "last_polled_at",
            "session_token",
            "state",
            "user_code",
        }
    ),
    "events": frozenset(
        {
            "agent_id",
            "created_at",
            "data_json",
            "event_id",
            "sequence_number",
            "session_id",
            "turn_id",
            "type",
        }
    ),
    "files": frozenset(
        {
            "account_id",
            "bytes",
            "created_at",
            "filename",
            "id",
            "mime_type",
            "path",
            "purpose",
        }
    ),
    "idempotency": frozenset(
        {
            "account_id",
            "created_at",
            "endpoint",
            "expires_at",
            "key",
            "request_hash",
            "response_json",
            "status",
        }
    ),
    "items": frozenset(
        {
            "agent_id",
            "content_json",
            "created_at",
            "id",
            "parent_id",
            "role",
            "seq",
            "session_id",
            "tokens",
            "turn_id",
            "type",
        }
    ),
    "journal": frozenset(
        {
            "agent_id",
            "at",
            "claimed_by",
            "depends_on",
            "detail_json",
            "id",
            "kind",
            "lease_until",
            "session_id",
            "status",
            "title",
        }
    ),
    "mcp_servers": frozenset(
        {
            "account_id",
            "created_at",
            "credential_service",
            "id",
            "last_seen",
            "name",
            "state",
            "tools_digest",
            "tools_json",
            "url",
        }
    ),
    "permission_grants": frozenset(
        {
            "account_id",
            "decision",
            "granted_at",
            "instruction",
            "permission",
            "profile",
            "source",
        }
    ),
    "probes": frozenset(
        {
            "account_id",
            "checked_at",
            "detail",
            "expires_at",
            "pack",
            "profile",
            "state",
        }
    ),
    "results": frozenset(
        {
            "count",
            "data_json",
            "id",
            "items_json",
            "kind",
            "notices_json",
            "operation",
            "session_id",
            "stored_at",
            "type",
        }
    ),
    "sessions": frozenset(
        {
            "account_id",
            "archived_at",
            "cost_micros",
            "created_at",
            "durability_mode",
            "forked_from_item",
            "harness_version",
            "id",
            "incognito",
            "input_policy",
            "input_tokens",
            "model",
            "output_tokens",
            "parent_session_id",
            "permission_mode",
            "persona",
            "profile",
            "status",
            "thinking_config",
            "title",
            "updated_at",
            "workspace_environment_id",
            "workspace_rel",
        }
    ),
    "steps": frozenset(
        {
            "created_at",
            "input_digest",
            "kind",
            "result_json",
            "session_id",
            "status",
            "step_id",
            "turn_id",
        }
    ),
    "subscriptions": frozenset(
        {
            "account_id",
            "capability",
            "created_at",
            "ended_at",
            "expires_at",
            "grant_id",
            "id",
            "objective",
            "profile",
            "result_json",
            "secret",
            "session_id",
            "sibling_id",
            "state",
            "wake",
            "work_id",
        }
    ),
    "turns": frozenset(
        {
            "cancel_requested",
            "cost_micros",
            "created_at",
            "error_code",
            "finished_at",
            "id",
            "input_json",
            "input_tokens",
            "iterations",
            "output_tokens",
            "session_id",
            "started_at",
            "status",
            "stop_reason",
            "termination",
        }
    ),
    "webhooks": frozenset({"account_id", "created_at", "id", "secret", "url"}),
}
"""Every table's columns as the first database that held it had them. Frozen on purpose: a
column added to `SCHEMA` and not here must be in `ADDED_COLUMNS`, or an existing database
never gains it."""


def test_a_column_added_to_the_schema_comes_with_its_migration() -> None:
    """The bug, named: `subscriptions.tags_json` was added to `SCHEMA` and, in a merge, lost
    from `ADDED_COLUMNS`. A new database had it and an existing one never would, so every
    watch opened on a running hub failed on the insert. Any column beyond a table's first
    ones must be a migration; a new table goes in `FIRST_COLUMNS` with all of its columns."""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == set(FIRST_COLUMNS), "a new table belongs in FIRST_COLUMNS"
        migrated = {(table, column) for table, column, _ in ADDED_COLUMNS}
        for table in sorted(tables):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            later = {(table, column) for column in columns - FIRST_COLUMNS[table]}
            assert later <= migrated, f"{sorted(later - migrated)} has no migration"
    finally:
        db.close()


async def test_a_subscriptions_table_from_before_tags_gains_the_column(tmp_path: Any) -> None:
    path = str(tmp_path / "before-tags.sqlite3")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA.replace(", tags_json TEXT", ""))
    worker = SqlWorker(path)
    try:
        await SessionStore(worker).initialize()

        def columns(db: Any) -> set[str]:
            return {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}

        assert "tags_json" in await worker.call(columns)
    finally:
        await worker.aclose()
