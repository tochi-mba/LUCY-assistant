# The tool layer

The model does not call tools one at a time. It answers with a **plan**: several steps,
where a later step names an earlier step's result by reference. The data those steps produce
never becomes tokens on its way between them.

That is the whole bet, and it is worth being concrete about what it saves. Searching the web
and then saving the three best results is, in a call-at-a-time world, an entire search result
rendered into the context so the model can copy three identifiers out of it and type them
into the next call. Here it is two steps and a `$hits[1,2,3]`. The page text is never a token
anybody paid for, and nothing was retyped, which is where the mistakes come from.

## The model never sees a service

It sees **capabilities** with product names — `music`, `research`, `workspace`, `notes` — and
never `spotify-api`, never a port, never an HTTP verb. Behind one capability there may be one
service, three, or none.

This is not presentation. A tool surface generated from an OpenAPI document leaks all three,
and it cannot express the thing a model most needs to be told, which is when *not* to call
something. Every operation here is hand-written for that reason, and a test greps the default
prompts for service names, ports and verbs.

Operations are **workflow-shaped, not route-shaped**. `music.findAndPlay` fans out to a
credential lookup and a provider internally. Exposing `getTrack`, `getAlbum` and `getArtist`
instead makes the model do the joining, in context, one round trip at a time — which is
exactly the cost plans exist to avoid.

## What this looks like in practice

Every example below is a real plan shape. The model writes the plan; everything after
`→` is what comes back.

### One question, two steps, nothing retyped

*"Find out when they're touring and put the dates in my notes."*

```json
{"steps": [
  {"id": "hits", "op": "research.search",
   "input": {"query": "2027 European tour dates", "limit": 5},
   "note": "Find out when the tour reaches Europe"},

  {"id": "saved", "op": "notes.remember",
   "input": {"title": "Tour dates", "body": "$hits[1]", "kind": "fact"},
   "note": "Keep the dates so I do not have to look them up again"}
]}
```

→ `hits` comes back as a collection of five, rendered as five labelled lines. `saved`
receives the first result **as data**, resolved from the stored result. The page text never
entered the conversation, and no identifier was copied by hand.

Both steps carry a `note`. That is what the person sees if the write needs approving, and
what the log says six weeks later.

### Three stores, three lists

*"What do we already know about them?"*

```json
{"steps": [
  {"id": "me", "op": "notes.aboutMe", "input": {},
   "note": "Load pinned blocks, remembered facts, and pinned account fields"}
]}
```

→ `me` comes back with **three keys**: `blocks` (memory), `facts` (ranked memories), and
`account` (pinned fields the person asked to keep in view). Those lists are not one ranking.
A later `notes.search` still queries memory only — mixing its scores with account pins
would hide a name the person asked to keep in view.

The product names stay `notes` and `account`. No host, no port, no `/v1/user`.

### Asking about a result without fetching it again

*"How many of those are things I told you, rather than things you worked out?"*

```json
{"steps": [
  {"id": "mine", "op": "notes.search",
   "input": {"query": "travel", "limit": 40},
   "note": "Find everything recorded about travel"},

  {"id": "stated", "op": "note.filter",
   "input": {"from": "$mine",
             "filters": [{"field": "trust", "op": "eq", "value": "stated"}]},
   "note": "Narrow that to the ones they told me directly"},

  {"id": "how_many", "op": "note.count",
   "input": {"from": "$stated"},
   "note": "Count them"}
]}
```

→ One network call. `note.filter` and `note.count` were generated because `notes.search`
returns notes, and they run against the stored result. Forty notes were fetched once and
never rendered twice.

`note.filter` refuses a field that was not declared, with a message naming it. That is
deliberate: filtering on a misspelled field would return everything, which looks like an
answer and is not one.

### The index, then one topic

*"What do I actually know about how they take their tea?"*

```json
{"steps": [
  {"id": "tea", "op": "notes.openTopic",
   "input": {"topic_id": "top_tea"},
   "note": "Expand the tea topic from the live index"}
]}
```

→ The live state already listed the topic. This call is the memories themselves, paid for
on purpose. Untrusted topics never appear in the index, so expanding one cannot smuggle
them in.

### Reads together, the write on its own

*"Check the two repositories and write me a summary."*

```json
{"steps": [
  {"id": "one", "op": "workspace.grep",
   "input": {"pattern": "TODO", "path": "service-a"},
   "note": "Find what is outstanding in the first service"},

  {"id": "two", "op": "workspace.grep",
   "input": {"pattern": "TODO", "path": "service-b"},
   "note": "Find what is outstanding in the second service"},

  {"id": "summary", "op": "workspace.write",
   "input": {"path": "outstanding.md", "from": ["$one", "$two"]},
   "note": "Write both lists into one file I can read later"}
]}
```

→ `one` and `two` run at the same time; they are reads and independent. `summary` waits for
both and runs alone, because it writes. Had the model put two writes in one plan, they would
have run one after the other in the order written — and two writes that could collide belong
in two plans, not one.

### A helper for work the parent does not need to see

*"Check both branches independently and tell me which one is actually ready."*

```json
{"steps": [
  {"id": "left", "op": "agents.spawn",
   "input": {"role": "reviewer",
             "objective": "Read the left branch and say whether it is ready to merge."},
   "note": "Review the left branch in a clean context"},

  {"id": "right", "op": "agents.spawn",
   "input": {"role": "reviewer",
             "objective": "Read the right branch and say whether it is ready to merge."},
   "note": "Review the right branch in a clean context"}
]}
```

A later turn can continue a finished helper without copying the whole brief:

```json
{"steps": [
  {"id": "again", "op": "agents.reopen",
   "input": {"id": "$left.agent_id",
             "guidance": "The merge landed; check whether the review still holds."},
   "note": "Continue the left review from where it stopped"}
]}
```

The new helper sees the previous items and last report. To hold the return to a shape, pass
`return_schema` on spawn. Mail between them is hop-counted, burst-capped, and identical
unread steers count as one.

→ Each spawn returns a handle immediately. The parent keeps talking. When a helper
finishes, a notice arrives at the next tool boundary; `work.result` is how the parent
reads the capped summary. Mid-run, `agents.message` queues a steer that the helper sees
before its next round, never mid-tool.

A helper cannot spawn another helper past the configured depth (default three), cannot
write, and cannot raise its own permission mode. Those are refusals in the tool result,
not crashes. `lucy.agent_max_concurrent` caps how many may run at once.

### A long command, without holding the turn open

*"Run the test suite. Tell me when it finishes."*

```json
{"steps": [
  {"id": "tests", "op": "workspace.run",
   "input": {"command": "make test", "wait": false},
   "note": "Start the suite; I will check in when it finishes"}
]}
```

→ A handle, immediately. The command keeps running. `work.check` names the state;
`work.wait` holds until it finishes or until its own deadline, and giving up does
**not** stop the command. `wait: true` (the default) still waits up to `wait_seconds`
and then returns the same handle if the command is still going. `wait_seconds: 0` is a
real deadline (return immediately with the handle), not "omit this and use the command
timeout".

### Named docs, loaded on purpose

*"How do I wait on a long job without polling?"*

```json
{"steps": [
  {"id": "index", "op": "help.skills", "input": {},
   "note": "See which docs I can load"},
  {"id": "page", "op": "help.skill",
   "input": {"name": "talking", "offset": 0, "limit": 80},
   "note": "Read the talking skill before I answer"}
]}
```

→ The same corpus an MCP client loads by digest. Windowed, with a showing-count. Prefer
these over guessing how a long job, an approval, or a memory write works.

### A capability that is not connected

```json
{"steps": [
  {"id": "play", "op": "music.play",
   "input": {"track": "$found[1]"},
   "note": "Start the song they asked for"}
]}
```

→ Nothing like this reaches the model, because an unconnected capability is **absent from
the registry**: `music.play` is not a tool it has. What it has instead is the capability
listed as needing connecting, and `capabilities.setup` to offer the link.

When a capability is connected but its credential has since been revoked, the call does run
and comes back as a result rather than an error:

```json
{"status": "connection_required",
 "service": "music",
 "profile": "personal",
 "scopes": ["user-modify-playback-state"],
 "connect_url": "…/connect?ticket=…",
 "message": "Music is not connected for profile 'personal'. Ask the person to open the
             link. Do not ask them for a password or a token."}
```

The model offers the link and carries on with the rest of the request. It does not retry, it
does not look for another route, and it does not ask for the credential itself.

### A result too large to show

```json
{"steps": [
  {"id": "log", "op": "workspace.read",
   "input": {"path": "build.log"},
   "note": "Read the build log to find why it failed"}
]}
```

→ The beginning and the end, with the middle elided and counted:

```
…
[... showing 1,190 of 41,203 tokens ...]
…
```

Head *and* tail, because errors cluster at the end of a log. To look somewhere specific, the
model runs the same step again with `show_from` set to a unique snippet it already saw:

```json
{"id": "detail", "op": "workspace.read",
 "input": {"path": "build.log", "show_from": "ModuleNotFoundError"},
 "note": "Look at the part of the log where the import failed"}
```

Nothing was lost: the whole result is stored and still addressable. What was bounded is how
much of it became tokens.

### Reading a file, then editing it without a stale write

`workspace.read` returns numbered lines and two fingerprints. `workspace.edit` walks a
ladder (exact, whitespace, fuzzy) and refuses if `fingerprint` does not equal the current
file digest.

```json
{"steps": [
  {"id": "seen", "op": "workspace.read",
   "input": {"path": "dates.txt", "start_line": 1, "limit": 80},
   "note": "Read the date list before changing it"},
  {"id": "fixed", "op": "workspace.edit",
   "input": {
     "path": "dates.txt",
     "old_string": "Berlin — 12 March",
     "new_string": "Berlin — 14 March",
     "fingerprint": "$seen.file_fingerprint"
   },
   "note": "Move the Berlin date by two days"}
]}
```

→ `seen` comes back as `1\t…` numbered lines plus `showing lines 1-80 of 80` and a
`file_fingerprint`. If another write landed first, `fixed` does **not** apply: it returns
`replaced: false` and names the fix — re-read, then reapply. Ambiguous `old_string` lists
every line number it matched. A near-miss shows the closest window as a diff.

The model never edits by line number. Line numbers in the read are for the person watching,
not a handle.

### A step that fails, in a plan that carries on

```json
{"steps": [
  {"id": "hits", "op": "research.search", "input": {"query": "tour dates"},
   "note": "Look up the dates"},
  {"id": "page", "op": "research.open", "input": {"hit": "$hits[1]"},
   "note": "Read the first result in full"},
  {"id": "note", "op": "notes.search", "input": {"query": "concerts"},
   "note": "Check what I already know about their gig preferences"}
]}
```

→ If `hits` fails, `page` is **skipped** — it depended on it — and says so. `note` still
runs, because it did not. The model gets a sentence about the failure and two useful
results, rather than nothing.

## Collections, and what declaring one buys

An operation that returns an opaque value tells the runtime nothing. It does not know the
result is a list, cannot label the entries, cannot resolve `$notes[2]`, cannot count, and can
only truncate silently. Every feature the library has is switched off by that one choice.

A **collection** is declared with three things, and each unlocks something:

| | |
| --- | --- |
| `label` | the one line an entry is rendered as. Usually all a model needs to pick which of forty things it wants. |
| `key` | a stable identity, so an entry can be named rather than described. |
| `fields` | what may be filtered, grouped and detailed by. Without it the free operations refuse outright, with a message saying so. |

`fields` is also the access boundary. A field that is not declared cannot be filtered on,
grouped by, or pulled out — so a record carrying an account id does not expose it merely by
passing through. The declarations live in one module, `packs/collections.py`, so that "does
any label expose something it should not" is a question somebody can answer by reading one
screen.

**Eight operations come free** for every collection in play: `filter`, `count`, `countBy`,
`distinct`, `mostCommon`, `first`, `pick`, `details`. They run against a **stored** result,
so *how many of those were confirmed* is arithmetic over something already fetched rather
than a second call and a second page of tokens.

They are generated only for collections something bound this turn actually produces.
Generating them for all seven would add dozens of tools a model cannot use, and selection
accuracy falls away sharply past thirty or forty. An unusable tool is not free; it is paid
for on every turn, in tokens and in wrong choices.

## References

`$stepId` is the whole result. `$stepId[1,3]` is specific positions, **1-based**, and they
index the full result rather than the lines that happened to be rendered — so a step can act
on something the model never actually read.

A field only accepts a reference if it was declared with `ref()`. An ordinary object field
refuses the string, which is the right refusal: a reference is meaningful only where the
operation said it resolves one.

## Every call says what it is for

Each step carries one plain sentence saying what *that* call is for — not a restatement of
its arguments. *"Discard the draft folder and start again"*, never
`workspace.delete(path=drafts)`.

It is written once and then pays for itself in six places: the progress line a person
watches, the approval prompt they answer, the transcript, the audit log, the summary that
survives a compaction, and the commit message on the checkpoint taken before a write. Nobody
can answer *"do you approve this?"* about a JSON blob.

When the model omits it, a deterministic fallback is rendered from the operation's own
description. A missing sentence is never an error; it is logged with a worse one.

## Reads run together, writes run in order

Read-only steps in one plan run concurrently. Anything with `effects: "write"` runs on its
own, after what it depends on, in the order written.

A plan containing a write is **refused whole** when the turn does not allow writes — before
anything runs, not part-way through. That is what makes a read-only mode trustworthy: it is
the default, not something each caller has to remember to pass.

When the mode is `ask` and no grant covers the write, the turn parks instead of failing.
An `approval_request` item names the permission in a sentence a person can answer. The next
`input.approval` on the one write path records a grant — once, this session, this profile,
or the whole account — and re-queues the same turn. The client's `approved: true` is an
input; the gate re-checks the ledger before the tool runs. A denial is a transcript item
and a grant the model will see as "not allowed", never an exception.

`auto` still asks for anything marked destructive or (when `approval_policy` is
`spend_and_destructive_ask`) anything that spends money. A stored grant can skip that
floor; the mode cannot. `notes.forget` is `notes.erase`. `workspace.delete` is
`workspace.destroy`. Those are separate from `notes.write` and `workspace.files` so
allowing ordinary writes does not also allow a delete.

## What a step is allowed to cost

| | |
| --- | --- |
| `read` | 2,000 tokens of a rendered result |
| `preview` | 400 — most of the time a model wants to know *which* of forty things it has |
| `total` | 8,000 for the whole plan's rendering |
| one result | hard-capped at 25,000 tokens before it spills |

The total bounds the **rendering**, not the data. Everything is still stored and still
addressable; what is bounded is how much of it becomes tokens. Overflow spills to the result
store and comes back as a reference, with exact counts — never dropped.

Step and plan timeouts are widened for capabilities that are legitimately slow. A long
extension job taking twelve seconds is not a bug, and failing it at ten only produces a
retry that also takes twelve.

## Failure

`failure` is `continue`: a dead service fails its own step, skips whatever depended on it,
and lets the model read a sentence about it. Four unrelated steps that already succeeded are
not thrown away.

A malformed plan comes back as **text describing what was wrong**, naming the step and the
field, and the model corrects it. Twice at most — a model that cannot answer the schema after
two tries has misunderstood the task, not the format.

Three identical calls with identical arguments is a model that has lost track, not one being
thorough. It is told what it already tried and what came back, which is usually enough; only
if it ignores that is the operation withdrawn for the rest of the turn.

## Not connected is a result, not an error

A capability without a credential returns a fixed, machine-readable body: the service, the
missing scopes, a connect link, and a sentence telling the model not to ask for a password.
A model handed a 502 apologises and retries. A model handed this offers the person a link.

For Lucy's own loop an unusable capability is **absent** — not in the registry, so it cannot
be called or half-called. Over MCP the tool stays **listed** with a "needs connecting"
description, because a client that cached a tool list has no way back from one that vanished.
Different audience, different answer.

## Where this lives

| | |
| --- | --- |
| `packs/base.py` | what a capability is: states, availability, permissions, setup |
| `packs/collections.py` | every collection, in one place |
| `packs/registry.py` | probing, deferral, the registry and runtime, the budgets |
| `packs/context.py` | what a handler is handed, and why no secret is reachable from it |
| `store/results.py` | the durable result store behind every reference |
