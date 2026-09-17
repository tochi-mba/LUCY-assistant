"""Every name Lucy's stream can say, written down in one place and all at once.

A taxonomy invented one event at a time never becomes coherent. The first twenty names get
a shape, the next twenty get a different one because a different person needed them on a
different afternoon, and a year later a client has to know that `tool_finished`,
`tool.complete` and `lucy.tool.done` are three spellings of two ideas. That cost is paid by
every client forever, and it cannot be undone without breaking them. So the whole catalogue
is declared here, at once, before the code that emits it exists.

## The grammar

    lucy.<domain>.<noun>.<verb>

The noun is elided when the domain *is* the noun: a session has nothing else to be about,
so `lucy.session.created` rather than `lucy.session.session.created`. A model has requests
and a cache, so `lucy.model.request.started` and `lucy.model.cache.hit` keep theirs. Every
segment is lower case, and `_` separates words inside a segment because `.` already means
something.

## The catalogue is open; the grammar is not

**New event types may be added at any time, and a client must ignore the ones it does not
recognise.** That is the contract, and it is what lets Lucy grow an event for something
that did not exist when a client was written without negotiating a version for each one.
The reverse promise is the useful half: a name, once emitted, keeps its meaning.

What is *not* open is the shape of a name. `is_well_formed` is checked where an event is
emitted rather than trusted, because a typo in an event type is invisible in the test that
asserts on the event it meant to write, and permanent in the log everything else replays
from.

## Why constants and not an enumeration

An enumeration would be a closed set, and this set is deliberately open. Constants also
survive what actually happens to a taxonomy: an event emitted in one module and consumed in
another, with the two agreeing through a name that a grep can find.

`GROUPS` is the authoritative index. It is what the tests enumerate and what a reference
page renders, and it is why this module has no `__all__` repeating a hundred and sixty-nine
strings that would drift from the constants above it within a week.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


GRAMMAR = re.compile(r"^lucy\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,2}$")
"""`lucy.` and then two or three segments -- three when the domain needs a noun of its own."""


# --------------------------------------------------------------------------------------
# Session. The conversation itself, and whether it is waiting on anybody. `requires_action`
# and `auth_required` are separate because the fix is different: one wants a person to
# answer a question, the other wants them to connect something.
# --------------------------------------------------------------------------------------

SESSION_CREATED = "lucy.session.created"
SESSION_UPDATED = "lucy.session.updated"
SESSION_IN_PROGRESS = "lucy.session.in_progress"
SESSION_IDLE = "lucy.session.idle"
SESSION_REQUIRES_ACTION = "lucy.session.requires_action"
SESSION_AUTH_REQUIRED = "lucy.session.auth_required"
SESSION_FORKED = "lucy.session.forked"
SESSION_RESUMED = "lucy.session.resumed"
SESSION_ARCHIVED = "lucy.session.archived"
SESSION_DELETED = "lucy.session.deleted"
SESSION_EXPIRED = "lucy.session.expired"
SESSION_HARNESS_VERSION_CHANGED = "lucy.session.harness_version_changed"


# --------------------------------------------------------------------------------------
# Turn. One request to think, which may contain many model calls. `superseded` is what an
# interrupt or a rollback leaves behind, and a client with no name for it shows a turn that
# simply stopped for no reason.
# --------------------------------------------------------------------------------------

TURN_CREATED = "lucy.turn.created"
TURN_QUEUED = "lucy.turn.queued"
TURN_STARTED = "lucy.turn.started"
TURN_IN_PROGRESS = "lucy.turn.in_progress"
TURN_COMPLETED = "lucy.turn.completed"
TURN_FAILED = "lucy.turn.failed"
TURN_CANCELLED = "lucy.turn.cancelled"
TURN_RETRYING = "lucy.turn.retrying"
TURN_SUPERSEDED = "lucy.turn.superseded"
TURN_BUDGET_WARNING = "lucy.turn.budget_warning"
TURN_BUDGET_EXHAUSTED = "lucy.turn.budget_exhausted"
TURN_MAX_ITERATIONS = "lucy.turn.max_iterations"


# --------------------------------------------------------------------------------------
# Model. A provider's bad afternoon, made visible. `overloaded` and `rate_limited` are not
# the same failure and must not be shown the same way: one is theirs and passes on its own,
# the other is ours and has a number attached.
# --------------------------------------------------------------------------------------

MODEL_REQUEST_STARTED = "lucy.model.request.started"
MODEL_REQUEST_RETRYING = "lucy.model.request.retrying"
MODEL_REQUEST_COMPLETED = "lucy.model.request.completed"
MODEL_REQUEST_FAILED = "lucy.model.request.failed"
MODEL_OVERLOADED = "lucy.model.overloaded"
MODEL_RATE_LIMITED = "lucy.model.rate_limited"
MODEL_REFUSED = "lucy.model.refused"
MODEL_CACHE_HIT = "lucy.model.cache.hit"
MODEL_CACHE_MISS = "lucy.model.cache.miss"
MODEL_STOP = "lucy.model.stop"


# --------------------------------------------------------------------------------------
# Content. What the person reads. Reasoning carries a display flag and is redactable,
# because a provider may hand back thinking that must not be shown verbatim.
# --------------------------------------------------------------------------------------

CONTENT_ITEM_ADDED = "lucy.content.item.added"
CONTENT_ITEM_DONE = "lucy.content.item.done"
CONTENT_TEXT_START = "lucy.content.text.start"
CONTENT_TEXT_DELTA = "lucy.content.text.delta"
CONTENT_TEXT_END = "lucy.content.text.end"
CONTENT_REASONING_START = "lucy.content.reasoning.start"
CONTENT_REASONING_DELTA = "lucy.content.reasoning.delta"
CONTENT_REASONING_END = "lucy.content.reasoning.end"
CONTENT_CITATION_ADDED = "lucy.content.citation.added"
CONTENT_REFUSAL = "lucy.content.refusal"


# --------------------------------------------------------------------------------------
# Plan and tools. Three domains, one story: the model answers with steps, the steps run,
# and what they returned is stored rather than pasted back into the window. `tool.skipped`
# is the one people leave out -- a step whose dependency failed did not itself fail, and
# saying so is the difference between a plan that reads as broken and one that reads as
# halted.
# --------------------------------------------------------------------------------------

PLAN_RECEIVED = "lucy.plan.received"
PLAN_INVALID = "lucy.plan.invalid"
PLAN_VALIDATED = "lucy.plan.validated"

TOOL_INPUT_START = "lucy.tool.input_start"
TOOL_INPUT_DELTA = "lucy.tool.input_delta"
TOOL_INPUT_AVAILABLE = "lucy.tool.input_available"
TOOL_STARTED = "lucy.tool.started"
TOOL_PROGRESS = "lucy.tool.progress"
TOOL_FINISHED = "lucy.tool.finished"
TOOL_FAILED = "lucy.tool.failed"
TOOL_SKIPPED = "lucy.tool.skipped"
TOOL_TIMEOUT = "lucy.tool.timeout"
TOOL_CANCELLED = "lucy.tool.cancelled"
TOOL_RETRIED = "lucy.tool.retried"
TOOL_TRUNCATED = "lucy.tool.truncated"
TOOL_SPILLED = "lucy.tool.spilled"
TOOL_REPETITION_DETECTED = "lucy.tool.repetition_detected"

RESULT_STORED = "lucy.result.stored"
RESULT_REFERENCED = "lucy.result.referenced"
RESULT_EVICTED = "lucy.result.evicted"


# --------------------------------------------------------------------------------------
# Capabilities and connections. Eight availability states need more than a connected flag,
# and each ending below is a different sentence to a person: `expired` asks them to
# reconnect, `insufficient_scope` names a missing scope, `pending` says they started and
# closed the tab.
# --------------------------------------------------------------------------------------

CAPABILITIES_SNAPSHOT = "lucy.capabilities.snapshot"
CAPABILITIES_CHANGED = "lucy.capabilities.changed"
CAPABILITY_PROBE_STARTED = "lucy.capability.probe.started"
CAPABILITY_PROBE_OK = "lucy.capability.probe.ok"
CAPABILITY_PROBE_FAILED = "lucy.capability.probe.failed"

CONNECTION_REQUIRED = "lucy.connection.required"
CONNECTION_AUTHORIZE_STARTED = "lucy.connection.authorize_started"
CONNECTION_PENDING = "lucy.connection.pending"
CONNECTION_COMPLETED = "lucy.connection.completed"
CONNECTION_FAILED = "lucy.connection.failed"
CONNECTION_EXPIRED = "lucy.connection.expired"
CONNECTION_REVOKED = "lucy.connection.revoked"
CONNECTION_INSUFFICIENT_SCOPE = "lucy.connection.insufficient_scope"
CONNECTION_REFRESH_STARTED = "lucy.connection.refresh.started"
CONNECTION_REFRESH_OK = "lucy.connection.refresh.ok"
CONNECTION_REFRESH_FAILED = "lucy.connection.refresh.failed"


# --------------------------------------------------------------------------------------
# Approvals and permissions. `auto_granted` carries the policy that granted it and which
# permission it matched: a person who never saw a prompt is owed the reason afterwards.
# --------------------------------------------------------------------------------------

APPROVAL_REQUESTED = "lucy.approval.requested"
APPROVAL_GRANTED = "lucy.approval.granted"
APPROVAL_DENIED = "lucy.approval.denied"
APPROVAL_AUTO_GRANTED = "lucy.approval.auto_granted"
APPROVAL_EXPIRED = "lucy.approval.expired"
APPROVAL_POLICY_CHANGED = "lucy.approval.policy_changed"
PERMISSION_MODE_CHANGED = "lucy.permission_mode.changed"


# --------------------------------------------------------------------------------------
# Agents. A child run is the hardest thing here to see from outside, so the refusals are
# events too: a spawn refused for depth, queued behind a concurrency cap or stopped by a
# budget is a fact about the system, and a run that silently did not happen is the worst
# kind of bug.
# --------------------------------------------------------------------------------------

AGENT_SPAWNED = "lucy.agent.spawned"
AGENT_STARTED = "lucy.agent.started"
AGENT_PROGRESS = "lucy.agent.progress"
AGENT_MESSAGE_SENT = "lucy.agent.message.sent"
AGENT_MESSAGE_DELIVERED = "lucy.agent.message.delivered"
AGENT_MESSAGE_REFUSED = "lucy.agent.message.refused"
AGENT_RESULT = "lucy.agent.result"
AGENT_FINISHED = "lucy.agent.finished"
AGENT_FAILED = "lucy.agent.failed"
AGENT_CANCELLED = "lucy.agent.cancelled"
AGENT_INTERRUPTED = "lucy.agent.interrupted"
AGENT_RESUMED = "lucy.agent.resumed"
AGENT_REAPED = "lucy.agent.reaped"
AGENT_DEPTH_REFUSED = "lucy.agent.depth_refused"
AGENT_CONCURRENCY_QUEUED = "lucy.agent.concurrency_queued"
AGENT_BUDGET_EXHAUSTED = "lucy.agent.budget_exhausted"


# --------------------------------------------------------------------------------------
# Journal. The blackboard siblings coordinate through. `lease_expired` is how a dead
# claimant's work comes back without anybody holding a lock, and `rejected` carries the
# feedback a veto hook wrote rather than only the refusal.
# --------------------------------------------------------------------------------------

TASK_CREATED = "lucy.task.created"
TASK_CLAIMED = "lucy.task.claimed"
TASK_PROGRESS = "lucy.task.progress"
TASK_COMPLETED = "lucy.task.completed"
TASK_BLOCKED = "lucy.task.blocked"
TASK_UNBLOCKED = "lucy.task.unblocked"
TASK_LEASE_EXPIRED = "lucy.task.lease_expired"
TASK_REJECTED = "lucy.task.rejected"
TASK_REASSIGNED = "lucy.task.reassigned"


# --------------------------------------------------------------------------------------
# Memory. `memory.written` is deliberate product design rather than telemetry: the person
# sees what Lucy learned as it learns it, which is the only thing that makes a permanent
# store feel like a memory instead of a file kept on them.
# --------------------------------------------------------------------------------------

MEMORY_RETRIEVED = "lucy.memory.retrieved"
MEMORY_WRITTEN = "lucy.memory.written"
MEMORY_UPDATED = "lucy.memory.updated"
MEMORY_SUPERSEDED = "lucy.memory.superseded"
MEMORY_FORGOTTEN = "lucy.memory.forgotten"
MEMORY_REJECTED = "lucy.memory.rejected"
MEMORY_DECAYED = "lucy.memory.decayed"
MEMORY_CONSOLIDATION_STARTED = "lucy.memory.consolidation.started"
MEMORY_CONSOLIDATION_FINISHED = "lucy.memory.consolidation.finished"


# --------------------------------------------------------------------------------------
# Context. The window, confessed. A band warning arrives while there is still room to act
# on it, which is the whole point of telling a model where it stands.
# --------------------------------------------------------------------------------------

CONTEXT_ASSEMBLED = "lucy.context.assembled"
CONTEXT_STATUS = "lucy.context.status"
CONTEXT_BAND_WARNING = "lucy.context.band_warning"
CONTEXT_TOOL_RESULTS_CLEARED = "lucy.context.tool_results_cleared"
CONTEXT_THINKING_CLEARED = "lucy.context.thinking_cleared"
COMPACTION_STARTED = "lucy.compaction.started"
COMPACTION_APPLIED = "lucy.compaction.applied"
COMPACTION_FAILED = "lucy.compaction.failed"
COMPACTION_DISABLED = "lucy.compaction.disabled"
CONTEXT_OVERFLOW = "lucy.context.overflow"


# --------------------------------------------------------------------------------------
# Workspace. A filesystem that is not permanent says so out loud: it warns on quota, says
# when it is expiring, and says when it went.
# --------------------------------------------------------------------------------------

WORKSPACE_PENDING = "lucy.workspace.pending"
WORKSPACE_READY = "lucy.workspace.ready"
WORKSPACE_FAILED = "lucy.workspace.failed"
WORKSPACE_FILE_CHANGED = "lucy.workspace.file_changed"
WORKSPACE_COMMAND_STARTED = "lucy.workspace.command.started"
WORKSPACE_COMMAND_OUTPUT = "lucy.workspace.command.output"
WORKSPACE_COMMAND_FINISHED = "lucy.workspace.command.finished"
WORKSPACE_CHECKPOINT_CREATED = "lucy.workspace.checkpoint.created"
WORKSPACE_REVERTED = "lucy.workspace.reverted"
WORKSPACE_QUOTA_WARNING = "lucy.workspace.quota_warning"
WORKSPACE_EXPIRING = "lucy.workspace.expiring"
WORKSPACE_ARCHIVED = "lucy.workspace.archived"


# --------------------------------------------------------------------------------------
# Files and artifacts. Separate domains because they have different owners: a file belongs
# to a person and outlives any session, an artifact belongs to the session that made it.
# --------------------------------------------------------------------------------------

FILE_UPLOADED = "lucy.file.uploaded"
FILE_DELETED = "lucy.file.deleted"
ARTIFACT_CREATED = "lucy.artifact.created"
ARTIFACT_DELETED = "lucy.artifact.deleted"


# --------------------------------------------------------------------------------------
# MCP. `tools.pin_mismatch` is the rug pull: a server whose tools changed under a pinned
# digest is announced to the person rather than adopted into a prompt on their behalf.
# --------------------------------------------------------------------------------------

MCP_SERVER_REGISTERED = "lucy.mcp.server.registered"
MCP_SERVER_UNREACHABLE = "lucy.mcp.server.unreachable"
MCP_TOOLS_IMPORTED = "lucy.mcp.tools.imported"
MCP_TOOLS_CHANGED = "lucy.mcp.tools.changed"
MCP_TOOLS_PIN_MISMATCH = "lucy.mcp.tools.pin_mismatch"
MCP_CLIENT_CONNECTED = "lucy.mcp.client.connected"
MCP_TASK_CREATED = "lucy.mcp.task.created"
MCP_TASK_UPDATED = "lucy.mcp.task.updated"
MCP_TASK_COMPLETED = "lucy.mcp.task.completed"


# --------------------------------------------------------------------------------------
# Usage. Cumulative, with children rolled up, because a bill nobody can watch arriving is a
# bill nobody can stop.
# --------------------------------------------------------------------------------------

USAGE_UPDATED = "lucy.usage.updated"
USAGE_BUDGET_WARNING = "lucy.usage.budget_warning"
USAGE_BUDGET_EXHAUSTED = "lucy.usage.budget_exhausted"


# --------------------------------------------------------------------------------------
# Security. Visible because silence here is the bug. Each one names what happened and never
# what it was about: the marker that matched, never the payload; the audience a token was
# minted for, never the token.
# --------------------------------------------------------------------------------------

SECURITY_INJECTION_SCRUBBED = "lucy.security.injection_scrubbed"
# The secret heuristic reads the name and not the value. Both of these are the name of an
# event *about* a credential and neither is one, which is the whole point of the pair.
SECURITY_SECRET_REDACTED = "lucy.security.secret_redacted"  # noqa: S105
SECURITY_SSRF_BLOCKED = "lucy.security.ssrf_blocked"
SECURITY_TOKEN_MINTED = "lucy.security.token_minted"  # noqa: S105
SECURITY_CONSENT_REQUIRED = "lucy.security.consent_required"


# --------------------------------------------------------------------------------------
# Stream. The transport talking about itself. These are the only events a client receives
# that were never written to the session's log: a snapshot is assembled for one connection,
# and a heartbeat exists so that a proxy does not decide a quiet turn is a dead one.
# --------------------------------------------------------------------------------------

STREAM_SNAPSHOT = "lucy.stream.snapshot"
STREAM_RESUMED = "lucy.stream.resumed"
STREAM_HEARTBEAT = "lucy.stream.heartbeat"
STREAM_ERROR = "lucy.stream.error"
STREAM_DONE = "lucy.stream.done"


GROUPS: Mapping[str, tuple[str, ...]] = {
    "Session": (
        SESSION_CREATED,
        SESSION_UPDATED,
        SESSION_IN_PROGRESS,
        SESSION_IDLE,
        SESSION_REQUIRES_ACTION,
        SESSION_AUTH_REQUIRED,
        SESSION_FORKED,
        SESSION_RESUMED,
        SESSION_ARCHIVED,
        SESSION_DELETED,
        SESSION_EXPIRED,
        SESSION_HARNESS_VERSION_CHANGED,
    ),
    "Turn": (
        TURN_CREATED,
        TURN_QUEUED,
        TURN_STARTED,
        TURN_IN_PROGRESS,
        TURN_COMPLETED,
        TURN_FAILED,
        TURN_CANCELLED,
        TURN_RETRYING,
        TURN_SUPERSEDED,
        TURN_BUDGET_WARNING,
        TURN_BUDGET_EXHAUSTED,
        TURN_MAX_ITERATIONS,
    ),
    "Model": (
        MODEL_REQUEST_STARTED,
        MODEL_REQUEST_RETRYING,
        MODEL_REQUEST_COMPLETED,
        MODEL_REQUEST_FAILED,
        MODEL_OVERLOADED,
        MODEL_RATE_LIMITED,
        MODEL_REFUSED,
        MODEL_CACHE_HIT,
        MODEL_CACHE_MISS,
        MODEL_STOP,
    ),
    "Content": (
        CONTENT_ITEM_ADDED,
        CONTENT_ITEM_DONE,
        CONTENT_TEXT_START,
        CONTENT_TEXT_DELTA,
        CONTENT_TEXT_END,
        CONTENT_REASONING_START,
        CONTENT_REASONING_DELTA,
        CONTENT_REASONING_END,
        CONTENT_CITATION_ADDED,
        CONTENT_REFUSAL,
    ),
    "Plan and tools": (
        PLAN_RECEIVED,
        PLAN_INVALID,
        PLAN_VALIDATED,
        TOOL_INPUT_START,
        TOOL_INPUT_DELTA,
        TOOL_INPUT_AVAILABLE,
        TOOL_STARTED,
        TOOL_PROGRESS,
        TOOL_FINISHED,
        TOOL_FAILED,
        TOOL_SKIPPED,
        TOOL_TIMEOUT,
        TOOL_CANCELLED,
        TOOL_RETRIED,
        TOOL_TRUNCATED,
        TOOL_SPILLED,
        TOOL_REPETITION_DETECTED,
        RESULT_STORED,
        RESULT_REFERENCED,
        RESULT_EVICTED,
    ),
    "Capabilities and connections": (
        CAPABILITIES_SNAPSHOT,
        CAPABILITIES_CHANGED,
        CAPABILITY_PROBE_STARTED,
        CAPABILITY_PROBE_OK,
        CAPABILITY_PROBE_FAILED,
        CONNECTION_REQUIRED,
        CONNECTION_AUTHORIZE_STARTED,
        CONNECTION_PENDING,
        CONNECTION_COMPLETED,
        CONNECTION_FAILED,
        CONNECTION_EXPIRED,
        CONNECTION_REVOKED,
        CONNECTION_INSUFFICIENT_SCOPE,
        CONNECTION_REFRESH_STARTED,
        CONNECTION_REFRESH_OK,
        CONNECTION_REFRESH_FAILED,
    ),
    "Approvals": (
        APPROVAL_REQUESTED,
        APPROVAL_GRANTED,
        APPROVAL_DENIED,
        APPROVAL_AUTO_GRANTED,
        APPROVAL_EXPIRED,
        APPROVAL_POLICY_CHANGED,
        PERMISSION_MODE_CHANGED,
    ),
    "Agents": (
        AGENT_SPAWNED,
        AGENT_STARTED,
        AGENT_PROGRESS,
        AGENT_MESSAGE_SENT,
        AGENT_MESSAGE_DELIVERED,
        AGENT_MESSAGE_REFUSED,
        AGENT_RESULT,
        AGENT_FINISHED,
        AGENT_FAILED,
        AGENT_CANCELLED,
        AGENT_INTERRUPTED,
        AGENT_RESUMED,
        AGENT_REAPED,
        AGENT_DEPTH_REFUSED,
        AGENT_CONCURRENCY_QUEUED,
        AGENT_BUDGET_EXHAUSTED,
    ),
    "Journal": (
        TASK_CREATED,
        TASK_CLAIMED,
        TASK_PROGRESS,
        TASK_COMPLETED,
        TASK_BLOCKED,
        TASK_UNBLOCKED,
        TASK_LEASE_EXPIRED,
        TASK_REJECTED,
        TASK_REASSIGNED,
    ),
    "Memory": (
        MEMORY_RETRIEVED,
        MEMORY_WRITTEN,
        MEMORY_UPDATED,
        MEMORY_SUPERSEDED,
        MEMORY_FORGOTTEN,
        MEMORY_REJECTED,
        MEMORY_DECAYED,
        MEMORY_CONSOLIDATION_STARTED,
        MEMORY_CONSOLIDATION_FINISHED,
    ),
    "Context": (
        CONTEXT_ASSEMBLED,
        CONTEXT_STATUS,
        CONTEXT_BAND_WARNING,
        CONTEXT_TOOL_RESULTS_CLEARED,
        CONTEXT_THINKING_CLEARED,
        COMPACTION_STARTED,
        COMPACTION_APPLIED,
        COMPACTION_FAILED,
        COMPACTION_DISABLED,
        CONTEXT_OVERFLOW,
    ),
    "Workspace": (
        WORKSPACE_PENDING,
        WORKSPACE_READY,
        WORKSPACE_FAILED,
        WORKSPACE_FILE_CHANGED,
        WORKSPACE_COMMAND_STARTED,
        WORKSPACE_COMMAND_OUTPUT,
        WORKSPACE_COMMAND_FINISHED,
        WORKSPACE_CHECKPOINT_CREATED,
        WORKSPACE_REVERTED,
        WORKSPACE_QUOTA_WARNING,
        WORKSPACE_EXPIRING,
        WORKSPACE_ARCHIVED,
    ),
    "Files": (
        FILE_UPLOADED,
        FILE_DELETED,
        ARTIFACT_CREATED,
        ARTIFACT_DELETED,
    ),
    "MCP": (
        MCP_SERVER_REGISTERED,
        MCP_SERVER_UNREACHABLE,
        MCP_TOOLS_IMPORTED,
        MCP_TOOLS_CHANGED,
        MCP_TOOLS_PIN_MISMATCH,
        MCP_CLIENT_CONNECTED,
        MCP_TASK_CREATED,
        MCP_TASK_UPDATED,
        MCP_TASK_COMPLETED,
    ),
    "Usage": (
        USAGE_UPDATED,
        USAGE_BUDGET_WARNING,
        USAGE_BUDGET_EXHAUSTED,
    ),
    "Security": (
        SECURITY_INJECTION_SCRUBBED,
        SECURITY_SECRET_REDACTED,
        SECURITY_SSRF_BLOCKED,
        SECURITY_TOKEN_MINTED,
        SECURITY_CONSENT_REQUIRED,
    ),
    "Stream": (
        STREAM_SNAPSHOT,
        STREAM_RESUMED,
        STREAM_HEARTBEAT,
        STREAM_ERROR,
        STREAM_DONE,
    ),
}
"""The catalogue, in the order the plan documents it. The index the tests enumerate."""

EVENT_TYPES = frozenset(name for names in GROUPS.values() for name in names)
"""Every declared name. Membership is informative and never a gate: the set is open."""

TRANSPORT_TYPES = frozenset(GROUPS["Stream"])
"""What a connection invents for itself and never writes to the session's log."""


def is_well_formed(name: str) -> bool:
    """Whether a name obeys the grammar. Checked where an event is emitted, not assumed."""
    return GRAMMAR.match(name) is not None


def is_declared(name: str) -> bool:
    """Whether a name is in this catalogue.

    Deliberately not used to accept or reject anything. A deployment shipping a pack with
    events of its own is expected, and a client that has never heard of one must ignore it
    rather than fail. This exists for documentation and for tests.
    """
    return name in EVENT_TYPES


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, as both the log and the wire see it.

    `sequence_number` is per session and monotonic, and it is the resumption cursor: a
    client that has seen 41 asks for everything after 41 and is made whole. `event_id` is
    the identity of the row, and it is what an audit trail or a bug report quotes, because
    a sequence number means nothing without saying which session it belongs to.

    `turn_id`, `agent_id` and `trace_id` are optional because they genuinely do not always
    apply. A session was created before any turn existed, a turn runs with no sub-agent,
    and a trace id arrives only when the caller was already carrying one. Sending them as
    nulls would teach a client to read absence as a value.
    """

    type: str
    sequence_number: int
    event_id: str
    session_id: str
    created_at: float
    data: Mapping[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    agent_id: str | None = None
    trace_id: str | None = None

    def envelope(self) -> dict[str, Any]:
        """The JSON body of one event. An optional id appears only when it applies."""
        body: dict[str, Any] = {
            "type": self.type,
            "sequence_number": self.sequence_number,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "data": dict(self.data),
        }
        optional = {
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "trace_id": self.trace_id,
        }
        body.update({name: value for name, value in optional.items() if value is not None})
        return body
