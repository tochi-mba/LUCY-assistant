# The hub, from the inside

`src/lucy_api/` is 27 packages, and each one's `__init__.py` says in a line what it is for.
This page gives them an order: one message followed from the front door to the reply, then
where to start when something goes wrong. Read it with the code open.

## One message, end to end

1. **In.** `POST /v1/sessions/{id}/inputs` (`api/routers/sessions.py`) queues a turn
   (`sessions/turns.py`, `submit_messages`) and, while it still holds the person's token,
   prepares what the turn may reach: settings, workspace, sibling access
   (`core/container.py`, `prepare_turn`). Nothing thinks yet; the request returns `202`.
2. **Claimed.** `turn/supervisor.py` claims queued turns in order, picks up what was prepared
   for each, and runs it -- with the session and turn ids bound onto every log line written
   while it runs.
3. **A round.** `turn/loop.py` (`run_turn`) builds the prompt (`turn/prompt.py`, then
   `context/`; the fixed text is `prompt/defaults/*.md`), asks the model (`model/`, one adapter
   per wire dialect), and reads the reply (`model/wire.py`, `said_and_planned`): words for the
   person, a plan of steps, or both.
4. **A plan runs.** `packs/service.py` (`Capabilities.execute`) puts it past the permission gate
   (`permissions/gate.py`: run, ask the person, or refuse), then runs each step: an operation in
   `packs/<capability>.py`, which reaches a sibling service through `clients/<sibling>.py`.
   Every step writes one log line (`packs/steplog.py`).
5. **Back to the model.** Results are trimmed to fit (`turn/window.py`), written to the
   transcript (`sessions/`), and streamed to the client (`stream/`). The loop goes round
   again until the model answers.
6. **Out.** A final reply that claims work nothing did is held back once
   (`turn/claims.py`). The turn is finished with its cost, and a failure is written into the
   transcript with its reason.

Two things happen beside that path:

- **Asking the person.** A write that needs approval parks the turn
  (`permissions/approvals.py`). The answer re-queues it, and the approved call runs, exactly as
  approved, before the model is asked anything.
- **Work that outlives a step** -- a long command, a watch, a helper -- lives in
  `work/registry.py` and announces itself when it ends. Helpers are a child run of the same
  loop (`agents/runtime.py`); a helper a restart interrupted is announced by
  `agents/restart.py`.

## Where to start when something goes wrong

| What you see | Start here |
| --- | --- |
| A turn failed, or was slow | `lucy logs --turn <id>`, then `turn/supervisor.py` |
| One step failed | `lucy logs --turn <id> --level warning`, then `packs/<capability>.py` and `clients/<sibling>.py` |
| Lucy asked too much, or not at all | `permissions/gate.py` and the `permission_mode` of the session |
| The model was told something odd | `GET /v1/sessions/{id}/context`, `context/`, `prompt/defaults/` |
| A helper stopped | `agents.list` in a conversation, `agents/runtime.py`, `agents/restart.py` |
| One provider behaves unlike the others | `model/<dialect>.py` and its row in `model/catalogue.py` |
| A conversation that used to work does not | `lucy eval` ([evals.md](evals.md)) |

## Rules that keep this readable

- Imports point downward: `api` → `turn` → `packs` → `clients`. The few that reach back up
  do it inside a function, with a comment saying why. `core/container.py` is the one place that
  knows everything, because it builds everything.
- A module answers one question, and its docstring says which, and why it exists.
- Nothing over 1,000 lines ([ADR 0005](adr/0005-no-file-over-1000-lines.md)).
- A test is named for the behaviour it holds, and a fix's test names the defect it pins.
