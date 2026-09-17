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

Step and plan timeouts are widened for capabilities that are legitimately slow. A media job
taking twelve seconds is not a bug, and failing it at ten only produces a retry that also
takes twelve.

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
