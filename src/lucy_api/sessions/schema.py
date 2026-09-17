"""Schema migrations for the hub's single SQLite database."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, profile TEXT NOT NULL,
 title TEXT NOT NULL, status TEXT NOT NULL, model TEXT NOT NULL,
 thinking_config TEXT NOT NULL, persona TEXT NOT NULL, parent_session_id TEXT,
 forked_from_item TEXT, workspace_environment_id TEXT, workspace_rel TEXT,
 harness_version TEXT NOT NULL, input_policy TEXT NOT NULL, durability_mode TEXT NOT NULL,
 permission_mode TEXT NOT NULL, incognito INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, archived_at REAL,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 cost_micros INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id,created_at,id);
CREATE TABLE IF NOT EXISTS turns (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 status TEXT NOT NULL, termination TEXT, stop_reason TEXT, started_at REAL,
 finished_at REAL, error_code TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
 output_tokens INTEGER NOT NULL DEFAULT 0, cost_micros INTEGER NOT NULL DEFAULT 0,
 iterations INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
 input_json TEXT NOT NULL, created_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL, parent_id TEXT REFERENCES items(id), turn_id TEXT,
 agent_id TEXT, type TEXT NOT NULL, role TEXT NOT NULL, content_json TEXT NOT NULL,
 tokens INTEGER NOT NULL, created_at REAL NOT NULL, UNIQUE(session_id,seq)
) STRICT;
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 sequence_number INTEGER NOT NULL, type TEXT NOT NULL, turn_id TEXT, agent_id TEXT,
 data_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(session_id,sequence_number)
) STRICT;
CREATE TABLE IF NOT EXISTS idempotency (
 key TEXT NOT NULL, account_id TEXT NOT NULL, endpoint TEXT NOT NULL, request_hash TEXT NOT NULL,
 status INTEGER NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, PRIMARY KEY(account_id,endpoint,key)
) STRICT;
CREATE TABLE IF NOT EXISTS steps (
 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 turn_id TEXT NOT NULL, step_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
 input_digest TEXT NOT NULL, result_json TEXT, created_at REAL NOT NULL,
 PRIMARY KEY(session_id,turn_id,step_id)
) STRICT;
CREATE TABLE IF NOT EXISTS compactions (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL, trigger_tokens INTEGER NOT NULL, model TEXT NOT NULL,
 prompt_version TEXT NOT NULL, summary TEXT NOT NULL, covers_from INTEGER NOT NULL,
 covers_to INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS audit (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
 session_id TEXT, turn_id TEXT, agent_id TEXT, action TEXT NOT NULL,
 detail_json TEXT NOT NULL, at REAL NOT NULL
) STRICT;
"""
