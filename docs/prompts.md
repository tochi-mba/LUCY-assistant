# Prompts

The model is given a prompt assembled from named sections, not a concatenated string that
grew as features landed. Sections are versioned. The transcript outlives the wording that
produced it, and `prompt_version` is a digest of every field of every section so a later
reader can answer "which prompt wrote this turn?" without reconstructing the files.

[docs/context.md](context.md) is the window, the bands, and the live state block. This page
is the standing text that sits in front of all of that.

## The twelve sections

Order is reading order. Trim order is `priority` (lower is kept longer).

| id | Band | What it is for |
| --- | --- | --- |
| `identity` | system | Who Lucy is. Persona standing data may fill this, as a reported claim. |
| `behaviour` | system | How it works for this person: style, permission mode, what is connected. |
| `tools` | system | The plan idiom. **Cannot be turned off.** Without it, plans stop composing. |
| `safety` | system | What it never does. **Cannot be turned off.** A page must not become instructions. |
| `lessons` | system | How this person works, as recorded notes, not as a second system prompt. |
| `helpers` | system | When to spawn a child, what a child may return, how to check in. |
| `workspace` | system | The sandbox: numbered reads, fingerprints, the confined tree. |
| `memory` | system | Notes are data. Untrusted notes stay out until confirmed. |
| `context` | system | The window is finite. References, not silent truncation. |
| `capabilities` | pinned | Names of what is ready this turn. Product names, never services. |
| `person` | pinned | What is recorded about the person, framed, shrinkable. |
| `goals` | pinned | Active goals, if any. |

`tools` and `safety` refuse a setting that would disable them. Everything else may be
replaced or dropped. A section that names a port, an HTTP verb, or a repository is a bug;
`tests/hub/test_prompt_sections.py` greps the defaults.

## Two HTTP views of the same assembly

`GET /v1/prompt/preview` renders the stable prefix with no transcript. Use it when a
section override has gone wrong, before spending a generation to find out.

`GET /v1/sessions/{id}/context` renders the prefix plus this session's projected history,
live feeds, and reclamation notices. It shares `SessionView` with the live supervisor so
the two cannot drift.

## Trust channels

Only authored instructions — the section bodies — use the provider's system channel.
Persona fields, pinned account facts, memory, tool results, live feeds, and the transcript
are data messages: third-person reported claims with provenance inline, inside a delimited
block. Concatenating a remembered note into the system prompt is how a web page from last
Tuesday becomes a standing order.

Live state (now playing, cwd, the memory topic index, work in flight) is rebuilt every
turn. A prompt preview must not consume the "just finished" flag a real turn still needs
to see.

## What a setting may reach

`lucy.response_style` changes `behaviour`. Prompt-feed masters and per-field toggles change
what the live block contains, not the section text. `prompt_allow_unknown_feed_fields` is
off by default and an assistant may never turn it on: a sibling inventing keys Lucy does
not know is otherwise a silent prompt injection surface.
