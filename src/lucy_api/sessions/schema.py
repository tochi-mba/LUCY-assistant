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

-- weftai's ResultStore, made durable. Its own default store is in-memory and tuned for one
-- chat turn; a session that lives for a week needs its `$hits` to still resolve tomorrow,
-- and a client needs to be able to read a result back without asking the model to fetch it
-- again. That is the whole value of plans: the data never goes back through the context.
CREATE TABLE IF NOT EXISTS results (
 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 id TEXT NOT NULL, operation TEXT NOT NULL, kind TEXT NOT NULL, type TEXT,
 count INTEGER, data_json TEXT NOT NULL, items_json TEXT, notices_json TEXT NOT NULL,
 stored_at REAL NOT NULL, PRIMARY KEY(session_id,id)
) STRICT;
CREATE INDEX IF NOT EXISTS results_age ON results(session_id,stored_at);

-- A child run. `delegation_json` is the typed brief rather than a one-line prompt: passing
-- only a sentence throws away the multi-turn reasoning that decided what to ask for.
CREATE TABLE IF NOT EXISTS agents (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 parent_agent_id TEXT, role TEXT NOT NULL, objective TEXT NOT NULL,
 delegation_json TEXT NOT NULL, status TEXT NOT NULL, depth INTEGER NOT NULL,
 tools_json TEXT NOT NULL, budget_json TEXT NOT NULL, workspace_rel TEXT,
 progress TEXT NOT NULL DEFAULT '', result_json TEXT, summary_tokens INTEGER NOT NULL DEFAULT 0,
 interrupted_reason TEXT, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 cost_micros INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX IF NOT EXISTS agents_session ON agents(session_id,status,created_at);

-- Messages between a parent and a child. Capped, counted and hop-limited from the first
-- version, because two models politely acknowledging each other is the default failure
-- rather than a hypothetical one.
CREATE TABLE IF NOT EXISTS agent_mail (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
 direction TEXT NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL,
 hops INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, delivered_at REAL
) STRICT;
CREATE INDEX IF NOT EXISTS agent_mail_inbox ON agent_mail(agent_id,delivered_at,created_at);

-- The blackboard. One sibling sees what another claimed and completed with no context
-- transferred between them, which is the cheapest coordination there is. A lease rather
-- than a lock, so a dead claimant's task comes back on its own.
CREATE TABLE IF NOT EXISTS journal (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 agent_id TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
 depends_on TEXT NOT NULL DEFAULT '', claimed_by TEXT, lease_until REAL,
 detail_json TEXT NOT NULL, at REAL NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS journal_open ON journal(session_id,status,id);

-- A decision a person made about a tool call, kept because a refusal that teaches is worth
-- more than one that only stops. `instruction` is the sentence they added when they said no.
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 turn_id TEXT, agent_id TEXT, operation TEXT NOT NULL, description TEXT NOT NULL,
 input_json TEXT NOT NULL, status TEXT NOT NULL, policy TEXT NOT NULL,
 lifetime TEXT NOT NULL DEFAULT 'once', instruction TEXT,
 requested_at REAL NOT NULL, decided_at REAL, decided_by TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS approvals_open ON approvals(session_id,status,requested_at);

-- What a person has granted without being asked again. Settings-api has no profile column
-- and no way to add one, and the scope asked for here is explicitly per-profile, so the
-- ledger lives where the profile does.
CREATE TABLE IF NOT EXISTS permission_grants (
 account_id TEXT NOT NULL, profile TEXT NOT NULL, permission TEXT NOT NULL,
 decision TEXT NOT NULL, instruction TEXT, granted_at REAL NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY(account_id,profile,permission)
) STRICT;

-- Probe results, cached per person and capability. Invalidated on connect, disconnect, a
-- settings change, and on the downstream errors that mean the answer just changed.
CREATE TABLE IF NOT EXISTS probes (
 account_id TEXT NOT NULL, profile TEXT NOT NULL, pack TEXT NOT NULL,
 state TEXT NOT NULL, detail TEXT NOT NULL, checked_at REAL NOT NULL, expires_at REAL NOT NULL,
 PRIMARY KEY(account_id,profile,pack)
) STRICT;

-- Uploads, and the things a run produced. Separate because they have different owners: a
-- file belongs to a person and outlives any session, an artifact belongs to the session.
CREATE TABLE IF NOT EXISTS files (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, filename TEXT NOT NULL, bytes INTEGER NOT NULL,
 mime_type TEXT NOT NULL, purpose TEXT NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS files_account ON files(account_id,created_at,id);
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 path TEXT NOT NULL, bytes INTEGER NOT NULL, mime_type TEXT NOT NULL,
 produced_by TEXT NOT NULL, created_at REAL NOT NULL
) STRICT;

-- An external MCP server a person registered. `tools_digest` is the anti-rug-pull pin: a
-- server that changes what its tools do is detectable, and the person is told rather than
-- the change being silently adopted into a prompt.
CREATE TABLE IF NOT EXISTS mcp_servers (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
 credential_service TEXT, state TEXT NOT NULL, tools_json TEXT NOT NULL,
 tools_digest TEXT NOT NULL, last_seen REAL, created_at REAL NOT NULL,
 UNIQUE(account_id,name)
) STRICT;

-- RFC 8628. A command line must never prompt for a password, so it prints a code and a URL
-- and waits. `user_code` is short enough to read aloud and is rate limited on lookup.
CREATE TABLE IF NOT EXISTS device_codes (
 device_code TEXT PRIMARY KEY, user_code TEXT NOT NULL UNIQUE, account_id TEXT,
 state TEXT NOT NULL, interval REAL NOT NULL, session_token TEXT,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, last_polled_at REAL
) STRICT;

-- Long-run push. The body is a signal (session, turn, status), never a transcript.
-- `secret` is shown once at create so the subscriber can check X-Lucy-Signature.
CREATE TABLE IF NOT EXISTS webhooks (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, url TEXT NOT NULL,
 secret TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(account_id,url)
) STRICT;
"""
