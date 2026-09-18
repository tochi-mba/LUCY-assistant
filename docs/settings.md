# Settings, through Lucy

Behind Lucy are eight services, each with its own settings namespace. A person should never
have to know that. They should be able to say *"stop remembering things on your own"* in a
conversation, or flip it in a client, without learning that memory has a namespace, that the
namespace is called `memory`, or that a service called settings-api exists at all.

That is the same rule the tool layer follows, applied to configuration: **the person and the
model see capabilities, never services.**

## Lucy proxies; settings-api decides

Lucy does not keep its own copy of anybody's settings. It reads the catalogue and the values
from settings-api, presents them grouped the way a person thinks about them, and writes back.
The catalogue it holds is a cache with a short life, and it is never the authority.

That split matters when a value is rejected. Lucy validates first, against the catalogue it
fetched, so a bad value comes back immediately with the bounds in the message instead of
after a round trip. Then settings-api validates again, because Lucy's copy may be stale and
because a client that talks to settings-api directly must get the same answer. **A validation
that only happens in the proxy is a validation that can be skipped.**

## Grouped by capability, not by service

`GET /v1/settings` returns every setting the person can see, grouped by the capability that
owns it. The mapping lives in `lucy_api.settings.groups` and nowhere else:

| Person sees | Namespaces | What is in it |
| --- | --- | --- |
| **Lucy** | `lucy`, `common` | Model, thinking, permissions, context budget, prompt-feed master switches, timezone, locale, units, default profile |
| **Account** | `user`, `keyring`, plus `lucy.feeds_account*` | Erasure, pinning, sessions, re-auth, and which pinned facts the model may see |
| **Persona** | `persona`, plus `lucy.feeds_persona*` | Default persona, pinned fields/notes, and whether identity and notes appear in the prompt |
| **Memory** | `memory` | Retrieval, trust floor, write floor, consolidation, erasure grace |
| **Music** | `spotify`, plus `lucy.feeds_music*` | Market, device, shuffle, repeat, and which now-playing lines the model may see |
| **Research** | `search`, plus `lucy.feeds_research*` | Providers, result count, recency, and whether the live block names the backend |
| **Workspace** | `environments`, plus `lucy.feeds_workspace*` | Idle TTLs, shell, history, command timeout, output cap, and which shell facts the model sees |
| **Installed extensions** | discovered namespaces and matching `lucy.feeds_*` keys | Settings contributed by operator-installed capabilities without naming them in the public family |

A pack does not invent a second mapping. Prompt-feed toggles are stored on `lucy` because
Lucy decides what the model sees; they are *shown* with the capability they describe, so
"hide now playing" sits next to "default speaker".

Two namespaces can legitimately hold the same key — `common.timezone` and `user.timezone` are
a deliberate collision, not a mistake. Lucy shows which one is in force and why, because a
person who sets a timezone and sees the old one still in use needs the answer to be visible
rather than something they have to infer.

## Account-wide vs profile-wide

A setting is either one value for the person or one value per keyring profile, never both.
Overlay of the same key would be a second settings system (which value wins?), and that is
the compromise settings-api refused. The catalogue declares the level; Lucy always passes
the session's profile, and account-scoped writes ignore it.

**Account-wide** (the unconsidered default): restrictions, spend, erasure, identity.
`search.disabled_providers`, `lucy.approval_policy`, `user.erasure_mode`,
`common.default_profile`, token ceilings. A work profile must not silently weaken a
promise made on the account.

**Profile-wide**: taste, routing, and anything whose correct answer depends on which
credential set is in use. `spotify.default_market`, `lucy.model`, `lucy.permission_mode`,
`search.safe_search`, prompt-feed toggles, which shell a workspace starts.

`describe_settings` reports `scope` on every entry. The model is told whether a change
is for this conversation's profile or for the person everywhere.

## Prompt feeds, as settings

Every line a sibling may put in front of the model is a boolean in the `lucy` namespace,
named `feeds_<capability>_<field>`. There is also a switch for the whole capability
(`feeds_music`) and three masters:

- `prompt_feeds_enabled` — off removes every feed
- `prompt_hide_personal_feeds` — incognito hides feeds marked personal (default on)
- `prompt_allow_unknown_feed_fields` — a sibling may invent keys Lucy does not know (default off, and an assistant may never turn it on)

Defaults hide the easy leaks (next track, last shell command, search backend, download
progress) and keep the lines that stop the model guessing (now playing, cwd, active job).
Unknown keys are dropped unless that last switch is on.

Turn execution is bounded by the lucy knobs the hub actually consumes. They are
resolved once when a turn is prepared, clamped, and held on `TurnPolicy`. Changing a
setting never moves the ceilings of a turn that is already running.

| setting | default | effect |
| --- | ---: | --- |
| `max_llm_turns` | 12 | Maximum model rounds in one main turn, including rounds after tool results |
| `max_subagent_turns` | 8 | Maximum model rounds for each child helper |
| `max_tool_calls_per_turn` | 60 | Maximum tool calls admitted during one main turn |
| `max_turn_seconds` | 0 | Wall-clock limit for a turn; zero means no deadline |
| `max_output_tokens_per_turn` | 8000 | How much the model may generate in one reply |
| `max_tool_result_tokens` | 25000 | Per-result cap before the rest spills and stays reachable by reference |
| `max_steps_per_plan` | 20 | Steps one plan may contain |
| `agent_max_depth` | 3 | How many levels of helper may nest |
| `agent_max_concurrent` | 5 | How many helpers may run at once |
| `memory_write_policy` | ask_first | `never` refuses new notes; `automatic` writes without asking |
| `permission_mode` | ask | Default for a new session when the create request omits it |
| `input_policy` | enqueue | Default for a new session when the create request omits it. `interrupt` stops the live turn and keeps its progress; `rollback` hides that turn's items from the next prompt |
| `max_context_tokens` | 200000 | The window the bands are shares of |
| `reserve_percent` | 13 | Empty share kept for the reply; written bands rescale around it |
| `warn_at_percent` | 60 | Fullness at which the prompt says the window is filling, before anything is dropped |
| `compaction_trigger_percent` | 72 | Fullness at which a live turn auto-compacts |
| `history_turns_kept` | 4 | Newest exchanges auto-compact may not summarise away |
| `tool_results_kept` | 3 | Newest unprotected tool results reclamation may not drop |
| `session_token_budget` | 0 | Tokens one conversation may spend; zero means no cap |
| `stream_thinking` | false | Whether reasoning events are forwarded to the client as they arrive |
| `log_message_content` | false | Whether this person's message bodies may appear on process log lines for the turn |
| `incognito` | false | Default for a new session when the create request omits it |

A new session that omits `model`, `thinking_config`, `permission_mode`, `input_policy` or
`incognito` takes those from this person's settings. An explicit field on the create
request wins. The session row is the live override after that.

`help`, `work` and `agents` cannot be listed in `disabled_capabilities`. Without help the
model cannot ask for the rest.

The store of record is settings-api's `lucy` catalogue. The hub's `context.fields` table
must stay in lockstep with it; a field that ships without a matching setting is a missing
toggle.

## What a setting looks like when Lucy shows it

Not a value. A value, its bounds, where it came from, and whether the person may change it:

| | |
| --- | --- |
| **value** | what is in force right now |
| **source** | their choice, an operator's policy, or the catalogue default |
| **bounds** | the range or choices — narrowed by operator policy where one applies, never the catalogue's wider range |
| **writable** | some settings are deliberately not theirs to change, and the reason is shown |
| **effect** | what changing it actually does, taken from the catalogue's own description |

The last one is the reason this is worth building rather than rendering a form from a JSON
schema. Every entry in the catalogue carries a description written for somebody deciding,
and throwing that away in favour of a label and a slider is throwing away the only part that
helps.

## When settings-api is unavailable

Every setting declares what a consumer must do when the service cannot be reached, and Lucy
honours it rather than treating an outage as a reason to guess.

A `use_default` setting falls back to its conservative value **inside a turn that is
allowed to run**. Two lucy keys refuse instead: `disabled_capabilities` and
`approval_policy`. Their defaults are permissive, so landing on them during an outage
would re-enable something the person turned off, or lower a floor, and nobody would find
out because the turn would succeed. Lucy therefore answers **503** with a stable
`settings-unavailable` problem and does not start the turn.

`vision_enabled` also refuses on outage, but only the image path should care. Until image
turns are gated, an outage of that one key does not block the whole turn.

An outage is visible, not silent: the person sees the 503 rather than discovering it from
behaviour.

## Edge cases, and the answer to each

**A namespace Lucy has no grant for** is not listed. That is an operator's deployment choice,
not an error, and showing a person a setting they cannot reach is worse than not showing it.

**A setting for a capability that is not connected yet** is shown, marked as taking effect
once it is connected. Answering "use Brave for search" before connecting search is a
perfectly reasonable order to do things in.

**A stored value that no longer validates** — because the catalogue narrowed a range under
it — is shown as needing attention, with the reason, and is **not** silently coerced. Quietly
moving somebody's choice to the nearest legal value is how a person ends up with a setting
they never chose.

**An operator policy that narrows a range** is what the person sees. Showing the catalogue's
wider range and then rejecting a value inside it is a worse experience than never offering
it.

**A setting only the account owner may write** is shown read-only to everybody else, with the
reason. Hiding it makes the behaviour it controls inexplicable.

**A deprecated setting** is shown with its successor, and a retired one is not shown at all —
it cannot affect anything, so it is noise.

**A write that names a profile other than the session's** is refused at Lucy. The model
does not pick a profile; the conversation already has one. Settings-api still requires
`?profile=` for profile-scoped keys and ignores it for account-scoped ones, which is why
Lucy can pass the session profile on every call.

**A concurrent write** from a client and a conversation at the same time takes the later one,
and both are recorded. Settings are small and last-writer-wins is honest; what is not
acceptable is one of them silently disappearing.

## The model's view

Three operations, and they are deliberately few:

    settings.describe(capability?)   what can be changed, what each one does, and whether it is account-wide or for this profile
    settings.get(name)               what it is now, where that came from, and which scope it has
    settings.set(name, value)        change it; the session's profile is used, never one the model invents

`settings.set` is a write, so it goes through the permission gate like any other. A model
changing a person's configuration without being asked is exactly the kind of thing that
should require a moment's consent, and the plain-language note that comes with every tool
call is what the person is shown: *"Turn off automatic memory writing"*, not a JSON patch.

The model never sees a namespace in a name it has to type. It writes `memory.write_policy`
because that is the capability and the knob; the mapping to a service is Lucy's problem.

## Events

Changing configuration is exactly the kind of thing somebody needs to be able to audit
afterwards, so it is noisy on purpose:

    lucy.settings.catalogue.refreshed   the cache was reloaded, with what changed
    lucy.settings.read                  which settings a turn depended on
    lucy.settings.changed               old value, new value, who, which profile
    lucy.settings.rejected              the value and why it was refused
    lucy.settings.degraded              settings-api was unreachable, and what was assumed
    lucy.settings.needs_attention       a stored value the catalogue no longer accepts

`lucy.settings.read` looks like noise until the first time somebody asks why an assistant
behaved differently on Tuesday. It answers that in one query.
