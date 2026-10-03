# Settings, through Lucy

Behind Lucy are nine services, each with its own settings namespace. A person should never
have to know that. They should be able to say *"stop remembering things on your own"* in a
conversation, or flip it in a client, without learning that memory has a namespace, that the
namespace is called `memory`, or that a service called settings-api exists at all.

That is the same rule the tool layer follows, applied to configuration: **the person and the
model see capabilities, never services.**

## Lucy proxies; settings-api decides

Lucy does not keep its own copy of anybody's settings. It reads the catalogue and the values
from settings-api on each call, presents them grouped the way a person thinks about them, and
writes back.

That split matters when a value is rejected. Settings-api validates every write, whoever
sends it, and its refusal -- with the bounds in the message -- is what the model reads. Lucy
does not validate first: a client that talks to settings-api directly must get the same
answer, and **a validation that only happens in the proxy is a validation that can be
skipped.**

## Grouped by capability, not by service

Every setting Lucy shows carries the capability that owns it: `settings.describe` places
each one under its capability for the model, and a client rendering a settings page uses the
same placement. The mapping lives in `lucy_api.settings.groups` and nowhere else:

| Person sees | Namespaces | What is in it |
| --- | --- | --- |
| **Lucy** | `lucy`, `common` | Model, thinking, permissions, context budget, prompt-feed master switches, timezone, locale, units, default profile |
| **Account** | `user`, `keyring`, plus `lucy.feeds_account*` | Erasure, pinning, sessions, re-auth, and which pinned facts the model may see |
| **Persona** | `persona`, plus `lucy.feeds_persona*` | Default persona, pinned fields/notes, and whether identity and notes appear in the prompt |
| **Memory** | `memory` | Retrieval, trust floor, write floor, consolidation, erasure grace |
| **Music** | `spotify`, plus `lucy.feeds_music*` | Market, device, shuffle, repeat, and which now-playing lines the model may see |
| **Research** | `search`, plus `lucy.feeds_research*` | Providers, result count, recency, and whether the live block names the backend |
| **Workspace** | `environments`, plus `lucy.feeds_workspace*` | Idle TTLs, shell, history, command timeout, output cap, and which shell facts the model sees |
| **Repositories** | `github` | Default owner and visibility for new repositories, merge method, branch clean-up, draft pull requests, how long a watch lasts ([repos.md](repos.md)) |
| **Installed extensions** | discovered namespaces and matching `lucy.feeds_*` keys | Settings contributed by operator-installed capabilities without naming them in the public family |

A pack does not invent a second mapping. Prompt-feed toggles are stored on `lucy` because
Lucy decides what the model sees; they are *shown* with the capability they describe, so
"hide now playing" sits next to "default speaker".

Two namespaces can legitimately hold the same key, so a setting is always named by both:
`common.timezone` is the `timezone` key in the `common` namespace. `settings.get` says
where the value in force came from, because a person who sets something and sees the old
value still in use needs the answer to be visible rather than something they have to infer.

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

`settings.describe` reports `scope` on every entry. The model is told whether a change
is for this conversation's profile or for the person everywhere.

## Prompt feeds, as settings

Every line a sibling may put in front of the model is a boolean in the `lucy` namespace,
named `feeds_<capability>_<field>`. There is also a switch for the whole capability
(`feeds_music`) and three masters:

- `prompt_feeds_enabled` — off removes every feed
- `prompt_hide_personal_feeds` — incognito hides feeds marked personal (default on)
- `prompt_allow_unknown_feed_fields` — a sibling may invent keys Lucy does not know (default off, and an assistant may never turn it on)

Defaults hide the easy leaks (next track, last shell command, search backend, download
progress) and keep the lines that stop the model guessing (now playing, git branch, active job).
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
| `fallback_model` | empty | Second `provider:model` tried once when the chosen model is unavailable |
| `max_thinking_tokens` | 0 | Hard ceiling on working-out; zero leaves the effort level to choose |
| `confirm_outward_actions` | true | Floor: anything other people will see asks first; auto cannot lower this |
| `enabled_capabilities` | empty | Empty stays quiet about disconnected capabilities in the prompt; naming one advertises its connect link. The HTTP catalogue still lists every pack |
| `auto_title` | true | Name a new conversation from the first user message |
| `notify_on_long_turn` | true | Emit `lucy.turn.slow` after `long_turn_seconds` |
| `long_turn_seconds` | 60 | Wait before that slow-turn event |
| `retry_attempts` | 2 | Extra tries for a failed sibling call when a repeat is safe: reads, and writes that never left or got a 429; 401 is never retried this way |
| `retry_max_seconds` | 30 | Total window those extra tries may use |
| `downstream_timeout_seconds` | 10 | Per-request wait for one sibling call |
| `agent_wall_clock_seconds` | 600 | Helper is stopped and told it ran out of time |
| `agent_message_max_chars` | 4000 | One helper message larger than this is refused |
| `agent_message_burst` | 5 | Unread helper messages allowed before the next is refused |
| `agent_max_depth` | 3 | How many levels of helper may nest |
| `agent_max_concurrent` | 5 | How many helpers may run at once; more wait in a queue as long again and start as these end |
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
| `max_parallel_steps` | 4 | How many steps of one plan may run at once |
| `step_timeout_seconds` | 10 | How long one step may take before it is marked timed out |
| `plan_timeout_seconds` | 60 | How long a whole plan may take |
| `render_read_tokens` | 2000 | How much of a stored result the formatter may read |
| `render_preview_tokens` | 400 | How much of each result is shown as a preview |
| `render_total_tokens` | 8000 | How much of one plan's rendered results may become tokens |
| `agent_result_token_cap` | 2000 | How much a helper may hand back when it is finished |
| `memory_retrieval_limit` | 12 | How many memory topics the live index may show |
| `workspace_retention_hours` | 24 | How long an idle workspace is assumed to last when the sandbox does not say |
| `session_idle_archive_days` | 30 | Idle days before an unused conversation is archived; zero never archives |
| `stream_thinking` | false | Whether reasoning events are forwarded to the client as they arrive |
| `log_message_content` | false | Whether this person's message bodies may appear on process log lines for the turn |
| `incognito` | false | Default for a new session when the create request omits it |

The rest of the namespace is the person's standing choices rather than ceilings: `model`
(`anthropic:claude-opus-5`), `thinking` (`medium`), `response_style` (`natural`),
`temperature` (`100`, in hundredths), `approval_policy` (`destructive_always_asks`),
`disabled_capabilities` (empty), `vision_enabled` (true), the three choices about how Lucy
works with the person and `prompt_sections_disabled` (below), the three prompt-feed masters
below, one `feeds_*` toggle per feed field, and the optional Laya switches
(`decisions` and `decision_*`, see [decisions.md](decisions.md)).

### When Lucy acts on her own

Four settings shape what happens when a watch, a repository watch or a check-in ends and
nobody is talking ([jobs.md](jobs.md)). Nobody is present then to read settings, so they are
read in the turn that opens the work and ride on it: as the subscription's tags and row,
which a restart keeps.

| setting | default | scope | effect |
| --- | --- | --- | --- |
| `act_unattended` | true | account | Off, no standing consent is recorded and the woken turn is told to say what happened and ask first. Consent recorded before it was turned off is not used either: the woken turn is prepared under the settings as they are when the work ends. An outage refuses rather than guess, like `approval_policy` |
| `quiet_hours` | empty | account | `HH:MM-HH:MM` on the person's clock (`common.timezone`), and it may wrap midnight (`23:00-07:00`). A wake inside it is held back as a check-in due when it closes; the `lucy.work.finished` event and the live-block line go out at once. Empty, or the same minute at both ends, is no window |
| `wake_by_default` | true | profile | What `watch.start`, `watch.command` and `repos.watch` do when the model does not pass `wake`. An explicit `wake` still wins |
| `watch_default_minutes` | 5 | profile | How long a watch lives when the model names no `for_seconds`; 1 to 60, and the hour ceiling stands |

When a watch or check-in opens under `act_unattended` off or under quiet hours, its tool
result says so ("ask before doing anything for them", "told at 07:00"), so the model does
not promise a 3am message or an unattended merge. A deferred wake is a check-in row: it
fires when the window closes even across a restart; it tells the person nothing if they
cancelled it or read the result in the meantime.

### What a sibling's settings supply when the model does not say

A person's choice in a sibling's namespace becomes the default a model's omission falls
to. The service owns the setting and the hub only reads it, once per turn, through
`settings.defaults.pack_defaults`. An outage here supplies nothing and never takes the
turn down; the hub's own default stands in. The two settings that refuse rather than fall
back, `memory.retrieval_trust_floor` and `github.merge_method`, are marked as not known
instead, and only the action that depends on them is refused. A field the model did name
always wins over the setting.

| setting | what it becomes |
| --- | --- |
| `search.default_result_count`, `search.search_backend` | How many results a search asks for, and the backend named on the live block |
| `spotify.default_device` | The device a music action plays on when it names none |
| `github.default_owner`, `github.default_visibility` | Where a new repository goes and who can see it |
| `github.merge_method` | How `repos.merge` lands a pull request when it names no `method`: `merge`, `squash` (the default) or `rebase`. A method that cannot be read is never guessed: a merge that names none is refused and asks for one, and a merge that names one goes ahead |
| `github.delete_branch_after_merge` | Whether `repos.merge` deletes the head branch when it names no `delete_branch`. Off by default; when the setting deletes it, the step says so |
| `github.draft_pull_requests` | Whether `repos.openPull` opens a draft when it names no `draft`. Off by default |
| `github.watch_default_hours` | How long `repos.watch` lasts when it names no `for_seconds`. One hour by default, held between an hour and a week |
| `environments.command_timeout_seconds` | How long `workspace.run` and `workspace.script` let a command run when they name no `timeout_ms`. Held to the hub's ten-minute ceiling |
| `environments.max_output_bytes` | How much of a command's output is captured. It narrows the hub's 64 KiB cap and never widens it |
| `memory.retrieval_limit` | How many memories `notes.search` brings back when it names no `limit`, and how many facts `notes.aboutMe` lists. Zero lists none; a search is still the asking, and brings back one |
| `memory.retrieval_trust_floor` | How far down the trust ladder a recall may reach: `stated`, then `observed`, then `inferred`. What is left out is counted in a notice. A floor that cannot be read is never guessed: the recall brings back nothing and says so |

### The person's own conventions

Five `common` settings, and two of Lucy's own, say how a person wants to be written to. Settings-api merges
`common` underneath every namespace, so they arrive with the `lucy` values, are read
once when a turn is prepared, and apply to helpers as they do to the main turn.

| setting | default | what the model is told |
| --- | --- | --- |
| `timezone` | `UTC` | The live block's `now` line is the person's clock, with the zone's name and its offset today: `2026-07-01 21:05 Europe/Lisbon, UTC+01:00 (Wednesday)`. "Remind me at nine" is then their nine |
| `locale` | none | Write in this language, with its spelling and its date and number formats; answer in another language when the person writes in one |
| `units` | `metric` | `imperial` says to give distances, weights and temperatures in imperial units |
| `time_format` | `24h` | `12h` says to write clock times as 2:20 pm |
| `currency` | none | Give costs in this currency |
| `lucy.formatting` | `auto` | `plain` says to write no Markdown, for a client that shows text as it arrives (a voice, an SMS); `markdown` says the client renders it |
| `lucy.emoji` | on | Off says to use none |

A setting left alone says nothing: the model follows how the person writes, which is the
better guide until somebody has chosen otherwise. What was chosen is its own prompt section,
`preferences`, so a choice is never the line cut to make room for prose. A value that cannot
be used (a zone the tz database does not have, a tag that is not a language tag) is treated
as not chosen.

### How Lucy works with the person

Three `lucy` settings are a matter of temperament rather than something one prompt can decide
for everybody. Each default is what the authored prompt already says, and a default says
nothing: a person who never opened settings is sent the prompt they always were, to the byte.

| setting | default | what the model is told when it is changed |
| --- | --- | --- |
| `ambiguity` | `assume_and_say` | `ask_first`: when a request could reasonably mean two things, ask which before acting, in one question. The default takes the careful reading and says which in one line |
| `opinions` | `when_they_matter` | `only_when_asked`: give an opinion only when asked, otherwise do what was asked |
| `announce_memory_writes` | on | Off: do not mention that something was kept unless asked. It is still the person's to read, correct and delete, and `memory_write_policy` still decides whether anything may be kept |

They are said in `preferences` after the person's conventions, as a paragraph that opens by
saying the person's choice wins where it differs from the general guidance, and never over
the safety rules or what needs approval. There is no setting for announcing a long step
first: the hub shows what the model wrote beside its steps only once they have run, so it
could not change what the person sees. `notify_on_long_turn` is the one that does. They are not
lines added to `behaviour` or `memory`: the line at the end of a section is the first one cut
at its ceiling, and a person may leave either section out. None of them is a floor; whether
something destructive or outward asks first is still the permission gate's decision. All four
are per profile, and an outage falls back to the default.

### Leaving parts of the prompt out

`lucy.prompt_sections_disabled` lists sections of the standing prompt this profile does not
want: any of `behaviour`, `lessons`, `helpers`, `workspace`, `memory` and `context`
([prompts.md](prompts.md)). Each costs tokens on every turn, and a person who never uses the
sandbox or helpers need not pay for their guidance. A dropped section is left out of what is
sent and out of what the window counts, for helpers as for the main turn and in
`GET /v1/sessions/{id}/context`.

`tools` and `safety` can never be left out, and neither can `identity`, `preferences`,
`capabilities` or `person`. Any other name in the list is ignored rather than failing the
turn. An assistant may never change this setting, even with approval: `settings.set` refuses
it, as it refuses every `lucy` key the catalogue marks `never`. Empty, the default, sends
every section, and an outage lands there too, which only adds guidance.

A new session that omits `model`, `thinking_config` (from the `thinking` setting),
`permission_mode`, `input_policy` or `incognito` takes those from this person's settings. An explicit field on the create
request wins. The session row is the live override after that.

`help`, `work` and `agents` cannot be listed in `disabled_capabilities`. Without help the
model cannot ask for the rest.

The store of record is settings-api's `lucy` catalogue. The hub's `context.fields` table
must stay in lockstep with it; a field that ships without a matching setting is a missing
toggle.

## What a setting looks like when Lucy shows it

Not a value. A value, where it came from, its scope, and -- from `settings.describe` -- its
bounds and what it does:

| | |
| --- | --- |
| **capability** | where it is shown, from `lucy_api.settings.groups` |
| **value** | what is in force right now |
| **set** / **source** | whether the person chose it, and where the value in force came from |
| **pinned** | an operator's policy fixed it, so it is not the person's to change |
| **scope** | account-wide or for this profile |
| **bounds** | the range or choices, as settings-api reports them (`settings.describe` only) |
| **summary** / **description** | what changing it actually does, taken from the catalogue (`settings.describe` only) |

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

These are the intended answers. Lucy enforces the profile rule itself today, because
`settings.set` has no profile field; the others depend on what settings-api returns, and
nothing Lucy returns yet marks a setting as needing attention, read-only or deprecated.

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

    settings.describe()                  what can be changed, grouped by capability, what each one does, and whether it is account-wide or for this profile
    settings.get(namespace, key)         what it is now, where that came from, and which scope it has
    settings.set(namespace, key, value)  change it; the session's profile is used, never one the model invents

`settings.set` is a write, so it goes through the permission gate like any other. A model
changing a person's configuration without being asked is exactly the kind of thing that
should require a moment's consent, and the plain-language note that comes with every tool
call is what the person is shown: *"Turn off automatic memory writing"*, not a JSON patch.

The model names a setting by the namespace and key `settings.describe` listed, such as
`lucy` and `memory_write_policy`. It never names a service, a host or a profile.

## Events

Changing configuration is exactly the kind of thing somebody needs to be able to audit
afterwards. None of these events is emitted yet; they are the planned set:

    lucy.settings.catalogue.refreshed   the cache was reloaded, with what changed
    lucy.settings.read                  which settings a turn depended on
    lucy.settings.changed               old value, new value, who, which profile
    lucy.settings.rejected              the value and why it was refused
    lucy.settings.degraded              settings-api was unreachable, and what was assumed
    lucy.settings.needs_attention       a stored value the catalogue no longer accepts

`lucy.settings.read` looks like noise until the first time somebody asks why an assistant
behaved differently on Tuesday. It answers that in one query.
