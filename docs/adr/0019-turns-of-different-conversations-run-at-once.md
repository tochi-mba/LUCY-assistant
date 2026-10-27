# ADR-0019: Turns of different conversations run at once

**Status:** accepted (2026-10-10).

## Context

The turn supervisor (`turn/supervisor.py`) claimed the oldest queued turn, ran it to the end,
and only then claimed the next. Nothing chose that. No ADR and no page in `docs/` decided that
the hub runs one turn at a time; it was the simplest loop, and it held while one person had
one conversation open.

It stopped holding once anything else ran beside that conversation. Measured on the live hub
on 2026-10-10, with a client that sends a message and times each event on the conversation's
stream: the message was accepted in **0.12 s** and its turn started **56.5 s** later. Another
conversation's turn, a background tester's, was running, and every turn of every conversation
of every account waited behind it: a woken turn, a second browser tab, anybody else.

The rule that matters was already kept elsewhere. `SessionStore.claim_next_turn` starts no
turn in a conversation that has one running, inside one SQLite transaction, so a conversation
has one writer whatever the supervisor does. Running turns of *different* conversations at once
is safe at the store.

## Decision

1. **Turns of different conversations run at once, up to `LUCY_MAX_CONCURRENT_TURNS`** (default
   four). Turns of one conversation still never do: the claim query is unchanged.
2. **One task claims; each turn is its own task.** The claimer fills the free slots with the
   oldest runnable turns, in the order they were queued, and stops when the queue or the slots
   run out. A turn that ends wakes it, so the slot it frees goes to the next queued turn.
   `wake()` still coalesces into the one claimer, and a wake that arrives while the claimer is
   asking the store is kept, so a turn queued at that moment is not left waiting for the next.
3. **A turn that raises is logged as `turn_crashed` and gives its slot back.** It used to end
   the drain loop, so the turns queued behind it waited for some other wake.
4. **Closing cancels every running turn.** Each is still `running` in the store, and the next
   process's start fails it as `process_restarted`, as it always did for the one.

## Consequences

- A conversation's turn waits only behind other conversations' turns, and only once four are
  running.
- Up to four turns call models, siblings and the decision service at once. The decision
  service keeps its own cap (`LUCY_LAYA_MAX_CONCURRENT`), and the database still has one writer
  thread, so turns' writes interleave rather than collide.
- The authority a request prepared for its turn (`PreparedTurn`) is still handed over by turn id
  and taken by that turn alone, and every line a turn logs still carries its own conversation
  and turn ids: both are per task, so turns running side by side do not see each other's.
- Helpers are not turns and take no slot. They run as work (`work/registry.py`) under their
  own `agent_max_concurrent`.
- The eval runner stays sequential on purpose: two conversations at once would measure
  contention as much as the model.

## What would change our minds

- **A model provider or a sibling that cannot take concurrent calls.** Setting
  `LUCY_MAX_CONCURRENT_TURNS=1` restores one turn at a time without a code change.
- **One account filling every slot.** The queue is first come, first served across accounts.
  If one person's background work starts holding others back, the claim takes accounts in
  turn instead.
- **More than one hub process.** The cap is per process, and so is the recovery: each
  process's start fails every turn left `running`, which would include the other's. Two
  processes would need a claim with a lease that expires.
