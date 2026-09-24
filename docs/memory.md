# Memory, as the model sees it

The model never talks to a memory service. It sees the **notes** capability: search,
expand a topic, record a fact, remember an episode, confirm, correct, forget. Behind that
is Memory-api, reached with a token Lucy minted for that audience, never with the caller's
token forwarded.

[Memory-api's own docs](https://github.com/tochi-mba/Memory-api) are the store. This page
is the fusion: what reaches the prompt, and what must not be mixed.

## Three lists, never one ranking

A turn may put three kinds of standing knowledge in front of the model. Their scores are
not comparable, so they are never merged:

1. **Persona** — who Lucy is in this profile. Identity and pinned notes, as a live feed.
2. **Account pins** — facts the person asked to keep in view, from the account record.
   A standing feed of their own, not mixed into retrieval.
3. **Memory retrieval** — the topic index from Memory-api, ranked, with untrusted topics
   held back. `notes.openTopic` expands one. `notes.search` is this store only.

`notes.aboutMe` returns `blocks`, `facts`, and `account` as separate lists. Combining them
into one "who is this person" ranking is how a pinned nickname outranks a confirmed medical
constraint, or the other way around, for a reason nobody can defend.

## The topic index is live state

Each turn fetches the topic list, ranks it, and puts the trusted prefix in the live block
with an exact count (`showing N of M`). A topic made entirely of untrusted memories never
appears: its title came from untrusted content, and the index goes into a prompt.

Incognito skips the fetch. `GET /v1/sessions/{id}/memory` returns the same trusted prefix,
or `[]` when incognito, or a notice when Memory-api is down. A downed store is not an empty
index pretending to be complete.

## Writes

| Operation | What it does |
| --- | --- |
| `notes.setFact` | A durable fact. Preference, constraint, identity. |
| `notes.remember` | An episode from this conversation. Stays with the session unless promoted. |
| `notes.confirm` | Vouch for an untrusted note so retrieval may use it. |
| `notes.correct` | Supersede, keeping history. Never delete-then-add. |
| `notes.forget` | Hidden immediately, erased after the grace period, restorable until then. |

## Lessons

A lesson is how this person wants the assistant to work: one imperative sentence, not a
fact about them. Lessons are Persona-api notes of kind `lesson`, written by the assistant
and pinned, so the persona feed carries them into every conversation as standing notes,
each marked `lesson:` with its `[ref ...]`. `lucy.feeds_persona_notes` turns that off.

| Operation | What it does |
| --- | --- |
| `notes.learn` | Keep a lesson. Refused past Persona-api's pinned-note cap, naming it. |
| `notes.reviseLesson` | Reword one by its ref, rather than keeping a second. |
| `notes.unlearn` | Stop following one. Persona-api's forget is a tombstone. |

Learning and rewording are `notes.write`; unlearning is `notes.erase`, which asks even
in `auto`. They are offered only where a Persona-api is configured, and never in an
incognito session.

`lucy.memory_write_policy` is `never`, `ask_first` (default), or `automatic`. Confirming is
always explicit: permanence is what makes a memory store worth attacking, so untrusted
notes are never auto-retrieved.

A credential-shaped string is refused at write time. The refusal names the vault and never
echoes the matched text.

## Boundary

Lucy calls Memory-api's two-credential `/v1/internal` surface: `LUCY_MEMORY_API_TOKEN` as
Bearer, a minted person token as subject proof. The person-facing `/v1/memory` routes are
not a confused-deputy path for the hub.

Notes results are protected from reclamation (`notes.` prefix). Throwing away the one
expansion the model just fetched is how an assistant forgets the person mid-sentence.
See [docs/context.md](context.md).
