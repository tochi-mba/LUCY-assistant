# Sessions

A session is one conversation. It is durable: closing a laptop, dropping SSE, or restarting
the process does not delete what was already said. The transcript is append-only items; a
turn is the unit of work that answers one input.

This page is the shape. [docs/api.md](api.md) lists every route. [docs/context.md](context.md)
is what a turn actually shows the model.

## Why items, not messages

A chat log of `user` / `assistant` cannot record a tool result, a parked approval, a
compaction, or a helper's return without pretending they are speech. Items are typed
(`message`, `tool_result`, `thinking`, `approval_request`, …) and ordered by `seq`. Nothing
is rewritten in place. A bad summary is a new compaction row; deactivating it restores the
turns it covered.

Helper items carry an `agent_id`. The parent prompt never sees them, and a helper never sees
the parent's. Mixing those two logs is how a child inherits instructions it was not given.

## The one write path

New work enters only through `POST /v1/sessions/{id}/inputs`. That route records a turn,
returns, and the in-process supervisor claims it. Closing the HTTP client does not cancel
the turn. Cancelling is `POST /v1/turns/{id}/cancel`, which is cooperative: a running turn
gets `cancel_requested` and ends itself after the current model round; a queued turn is
cancelled immediately.

The request carries an `Idempotency-Key`. A retry with the same key and the same body is
the original turn, not a second one.

## What happens if you speak while it is still working

`input_policy` on the session, defaulting from `lucy.input_policy`:

| Policy | Live predecessor | Next prompt |
| --- | --- | --- |
| `enqueue` | Continues. The new input waits. | Both turns, in conversational order. |
| `reject` | Continues. The new input is refused. | Unchanged. |
| `interrupt` | Asked to stop. Progress already made stays. | The interrupted items, then the new input. |
| `rollback` | Asked to stop. Its items are hidden. | The new input only. |

Interrupt and rollback both emit `lucy.turn.superseded`. Rollback is the only policy that
drops the superseded turn from projection; the rows remain in the transcript so "why did it
think that?" stays answerable.

A cancel check runs before each model round **and** after it. Streaming uses `stream()`, so
a speaking reply would otherwise finish successfully while a person had already asked it to
stop. `finish_turn` keeps an interrupt or rollback `stop_reason` even when the loop reports
`cancelled`.

## Changing a session while it is working

A session carries the settings a person can reasonably change mid-conversation: `title`,
`input_policy`, `permission_mode`, `archived`, and `disabled_capabilities` -- this one
conversation's own list, kept beside the profile's `lucy.disabled_capabilities` and only
ever adding to it. `["agents"]` is how "no helpers in this conversation" is said.

The policy, the mode and the disabled list change what a running turn may do, and they take
effect **on the running turn** at its next model round: the mode the gate reads, the
capability list the live block shows, and the plan schema the model is offered are all
rebuilt. That is a thing a person should have to mean, so `PATCH /v1/sessions/{id}` with one
of those fields while a turn is live is answered with a **409** whose detail names the turn,
the fields and the two answers. The caller sends the same change again with:

| `apply` | What happens |
| --- | --- |
| `"now"` | Applied. The `lucy.session.updated` event carries `during_turn` with the turn id. |
| `"after_turn"` | Held. `lucy.session.change_held` is emitted, `pending_changes` shows it on the session, and it lands when the turn ends with an `updated` event marked `held`. |

A title or an archive flag changes at once whatever is running. With no turn live, `apply`
is unnecessary and ignored. A parked turn (`input_required`) counts as live, and nothing
held is spent for it: the change lands when the turn actually ends.

## Workspace

Every successful create and every fork gets its own confined `sessions/<id>` subtree inside
a stable environment for that account and profile. A retry reuses the same environment
instead of consuming another slot. A fork does **not** share the parent's files. Setup
failure after the environment was created deletes that partial environment and answers 503,
so the same idempotency key can finish the original session later.

`progress.md`, `tasks.json`, and a best-effort git baseline are seeded on attach.
[docs/tools.md](tools.md) covers the read/edit ladder.

## Context, compaction, restart

`GET /v1/sessions/{id}/context` assembles the same prompt a live turn would see, including
the reclamation filter, and does **not** write a compaction. Auto-compact happens once per
live turn when the window crosses `compaction_trigger_percent`, keeping `history_turns_kept`
recent turns verbatim. `POST /v1/sessions/{id}/compact` and `uncompact` are the explicit
versions of the same projection and keep the same window.

A process restart fails turns left `running` (a tool that already ran must not run again)
and then drains what was still queued. Turns parked on a person (`input_required`,
`auth_required`) survive.
