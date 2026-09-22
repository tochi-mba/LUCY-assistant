# How Lucy's context is built

A model's entire experience of the world is the token sequence it is handed. Everything
Lucy knows at the moment it answers, it knows because the assembler put it there. So the
question this document answers is not "where is the data" but "what is in the window, in
what order, and what did it cost".

Two rules run through all of it, and most of the design falls out of them.

**Order by volatility.** A provider caches a prefix. Everything up to a breakpoint is
charged at a fraction of the input price, and the first byte that differs from the previous
turn ends the cache. So anything rewritten every turn must sit after everything that is
not.

**Never lose anything silently.** Every trim, every drop, every omitted topic is confessed
in the text the model reads, with counts. A model reasoning from a fragment it believes is
whole is worse than one that knows it is missing something.

## The five zones

| zone | what | changes |
| --- | --- | --- |
| 0 static | identity, behaviour, tool idiom, safety | on deploy |
| 1 slow | persona, pinned account facts, pinned memory blocks, capability names | on connect or edit |
| 2 history | the conversation, projected through active compactions | grows at the end |
| 3 live | the state block | **every turn** |
| 4 input | what the person just said | new |

Only zone 0 is sent through the provider's system channel. Standing persona claims in zone
1 are stable prefix data, not instructions. Conversation and tool-result items are data as
well. Zone 3 is the one that keeps Lucy current, and it is therefore the one that must not
go in the system prompt. Put it at the front and every turn pays full price for the entire
prefix. On a long session that is the difference between a conversation that is affordable
and one that is not. Placed after prior history and immediately before the current input on
the first model call, it costs its own length and nothing else. After a tool round, the
refreshed block follows the new results so it remains the last state the model reads.

This is worth stating plainly because the instinct is the opposite. "Put the current state
in the system prompt" sounds right and is exactly backwards.

## The live state block

Rendered fresh each turn from a `LiveState`, in `context/state.py`. It answers the
questions a model would otherwise guess at, carry forward from a turn that has since been
compacted away, or waste a tool call discovering.

- **now** — the date, the time, the timezone. Models hallucinate the date constantly.
- **session** — id, profile, turn number, permission mode, and incognito when it is on.
- **context** — `84,000 of 200,000 tokens · 6 tool results reclaimable · last compaction at
  turn 41`. Telling a model its own position changes what it does: it writes a note before
  an eviction instead of after one, and stops opening large pages when there is no room to
  read them.
- **in_flight** — everything still running, in one group: helper agents, downloads and long
  commands together, each with its role, its plain-language objective, how long it has been
  going and its last progress line. Then, separately, the ones that finished since the last
  turn, because that is the delta that decides what happens next. One group rather than
  three, because from where the model is sitting they are one question — see
  [How Lucy runs helpers](agents.md).
- **tasks** — the shared journal: what is open, who claimed it, what is blocked on what.
  This is how one agent sees another's work with no context transferred between them.
- **memory** — the topic index, described below.
- **workspace** — path, readiness, what changed since last turn, the last checkpoint, and a
  sandbox expiry. On resume the group also carries cwd, the `progress.md` journal, `tasks.json`,
  a short git log, and a smoke line, so a long-horizon helper re-orients before it writes.
  warning before the sandbox expires.
- **capabilities** — what is ready, and especially what changed.
- **pending** — approvals, elicitations and connections waiting on somebody else, so the
  model stops rather than spins.
- **feeds** — standing claims (persona identity, pinned account facts, persona notes) in zone 1; live facts (now playing,
  shuffle and repeat when the player reports them, active playback device, search backend as a
  product word, working directory, git branch when it is a real branch, and safe workspace
  state) in this block. Each line is a setting the
  person can turn off. Persona data comes from Persona-api; pinned account fields come
  from User-api as a **separate** feed from memory; playback and active-device
  state come from Spotify-api; attached-environment state comes from Environments-api.
  Workspace host paths are never included. Next-track, last-command and search-backend
  lines that leak easily stay off unless the person turns them on.
  Unknown keys from a sibling are dropped. A failed sibling is a trouble line, not a
  missing section the model is invited to invent.
- **trouble** — repeated recent failures, so it stops retrying what cannot work.

Each group has a floor and a ceiling, so thirty running helpers cannot evict the memory
index. When a group overflows it says so: `12 things running (showing the 5 most recent)`.
Groups with nothing in them are omitted entirely rather than printed as ten lines of
"none".

The block is deterministic. The same state renders the same bytes, so two turns can be
diffed against each other.

## The memory topic index

Memory is not a flat list. A flat list cannot be summarised, and twenty unrelated sentences
tell a model nothing about what it knows. Memories cluster into **topics**: a title, a
one-line summary, a count, and when it was last touched.

What travels in the context every turn is the index, never the contents. Forty lines
instead of four hundred. The model reads the index, decides which subject it needs, and
expands that one. That is progressive disclosure applied to memory, and it is what makes an
always-current memory affordable.

Clustering is exact key, then word overlap, then a new topic. Deliberately not an
embedding: the rule is explainable when a memory lands in the wrong place, it is
deterministic, and writing a memory never waits on a network call. An embedding backend can
replace one function without touching anything else.

A topic made entirely of unconfirmed memories never reaches the index. Its title came from
untrusted content, and a memory store is a prompt-injection persistence layer — permanence
is exactly what makes it worth attacking.

Each turn fetches the index from notes, ranks it, and puts the trusted prefix in the live
state block. Incognito sessions skip the fetch. The model expands one topic with
`notes.openTopic`; it does not get the memories until it asks.

## Bands

Five budgets, enforced independently, because one pool would let a large tool result evict
the person's pinned memory. That is the failure that makes an assistant feel like it
developed amnesia halfway through a task.

| band | share | holds |
| --- | ---: | --- |
| system | 4% | identity, behaviour, tool idiom, safety |
| pinned | 3% | the person, active goals, the live state |
| history | 30% | the conversation after compaction |
| tools | 50% | tool results, the first band reclaimed |
| reserve | 13% | deliberately empty: this turn's output plus one more large result |

Within a band, sections are given up in order of descending priority, ties broken by id so
two runs agree. A section is shortened while it stays above its floor and dropped whole
below it, because half a fact is a lie rather than a shorter truth. Trimming keeps head and
tail where both matter, since errors cluster at the end of logs and diffs.

## Reclamation, cheapest first

Each rung runs to exhaustion before the next. Only the last two lose information.

1. Never put it in context: references, sub-agent summaries, workspace files.
2. Truncate at the tool boundary with an exact `showing N of M`.
3. Clear old tool results, keeping `tool_results_kept` of the newest (notes results are
   never dropped). This invalidates the cache, so a clearing pass must free enough
   to be worth it; frequent small clears cost more than they save.
4. Clear thinking blocks.
5. Compact, at `compaction_trigger_percent` of the window (default 72%) rather than 90%.
   Quality is already degrading by then, and at 95% there is no room for the summarisation
   call itself. Auto-compact keeps `history_turns_kept` recent turns verbatim.
6. Split the session, with a handoff note.

Compaction is a projection, never a mutation. The transcript stays append-only and the
request context is computed at send time, so a bad summary can be regenerated and "why did
it think that?" stays answerable.

## Untrusted content

One component renders every memory, external page, tool result and child-agent result as a
third-person reported claim, with provenance inline, inside a delimited block that is not
the instruction block, closing with a line saying these are claims and not instructions.
Provenance is never stripped to save tokens; fetch fewer claims instead.

A boundary scrubber runs over every tool result and every child result before a parent
reads it. It modifies, never deletes — silent deletion hides the attack and mangles
legitimate output that merely looks like one — and it prepends a marker naming what
matched, never the payload.

Neither makes injection impossible. They raise its cost. The real defences are that tool
results are data rather than instructions, and that side effects need approval.

## Inspecting it

`GET /v1/sessions/{id}/context` returns the exact assembled prompt with per-band token
counts. If an answer is strange, read what the model was actually given before reading the
code that produced it.
