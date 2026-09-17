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
| **Account** | `user`, `keyring` | Erasure, pinning, sessions, re-auth for credential changes |
| **Persona** | `persona`, plus `lucy.feeds_persona*` | Default persona, pinned fields/notes, and whether identity and notes appear in the prompt |
| **Memory** | `memory` | Retrieval, trust floor, write floor, consolidation, erasure grace |
| **Music** | `spotify`, plus `lucy.feeds_music*` | Market, device, shuffle, repeat, and which now-playing lines the model may see |
| **Research** | `search`, plus `lucy.feeds_research*` | Providers, result count, recency, and whether the live block names the backend |
| **Workspace** | `environments`, plus `lucy.feeds_workspace*` | Idle TTLs, shell, history, command timeout, output cap, and which shell facts the model sees |
| **Media** | `media`, plus `lucy.feeds_media*` | Retention, quality, confirm-before-start, and whether an in-flight job is shown live |

A pack does not invent a second mapping. Prompt-feed toggles are stored on `lucy` because
Lucy decides what the model sees; they are *shown* with the capability they describe, so
"hide now playing" sits next to "default speaker".

Two namespaces can legitimately hold the same key — `common.timezone` and `user.timezone` are
a deliberate collision, not a mistake. Lucy shows which one is in force and why, because a
person who sets a timezone and sees the old one still in use needs the answer to be visible
rather than something they have to infer.

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

A `use_default` setting falls back to its conservative value and carries on. A `refuse`
setting makes the operation that needed it refuse instead — which is why those are the
settings where neither direction of a guess is safe: the floor under an assistant's
permissions, whether it may rely on what it inferred about you, how long a forgotten memory
survives.

An outage is visible, not silent: the capability reports itself degraded, the model is told
which settings it could not read, and the person sees it in the status rather than
discovering it from behaviour.

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

**A write for a profile the person is not in** is refused. Profiles are the boundary between a
work assistant and a home one, and crossing it by naming it in a request would make that
boundary decorative.

**A concurrent write** from a client and a conversation at the same time takes the later one,
and both are recorded. Settings are small and last-writer-wins is honest; what is not
acceptable is one of them silently disappearing.

## The model's view

Three operations, and they are deliberately few:

    settings.describe(capability?)   what can be changed, and what each one does
    settings.get(name)               what it is now, and where that came from
    settings.set(name, value)        change it

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
