# ADR-0018: Lucy's web client is a sibling repository that reads the native stream

**Status:** accepted (2026-10-09). Built: [LUCY-ui](https://github.com/tochi-mba/LUCY-ui) 0.1.0.

## Context

Until now the hub had one client, `lucy talk`. The owner asked for a browser client built on
Vue, with an open-source animated face as Lucy's face, streamed text, approval cards, and a way
to watch, in the browser, a conversation that a coding agent such as Claude Code is having with
Lucy from a terminal ("headed").

Everything such a client needs already existed: the one write path, the resumable event stream
with its snapshot, typed items including approval requests, the device sign-in flow, and the
context report. The questions were where the client lives, which of the hub's two stream
encodings it reads, how a browser reaches a hub that has no CORS policy, and how a conversation
driven from a terminal is told apart.

## Decision

1. **A sibling repository, `tochi-mba/LUCY-ui`, not part of this one.** It is a client with its
   own toolchain (Node, Vue, Playwright), not a family service: no keyring audience, no image, no
   row in `repos.txt`, no parity check. Like clyde, it is held to the family's bar with its own
   CI: 100% coverage per file and end-to-end tests in three engines against a fake hub.
2. **The native `lucy.*` stream, not the AI SDK projection.** The projection carries an approval
   only as an id, and drops the items as they land, the context report and finished work. The
   client needs all four, and reads them as `lucy talk` does.
3. **A same-origin proxy, not CORS.** The client's dev server and its `serve` script pass `/v1`,
   `/healthy`, `/ready` and `/device` through to `LUCY_URL`, on loopback. The hub is unchanged.
4. **Headed conversations are named, not flagged.** The input envelope has no author field, so
   the client's headed script titles every conversation it starts `Claude Code · <topic>`, and
   the client labels that conversation's human side "Claude Code". The person answers the
   conversation's approval cards in the browser; the terminal is told a card is waiting.
5. **Sign-in is the device flow.** The page shows a code and `lucy approve CODE` from a
   signed-in terminal hands over that sign-in. The token stays in the tab's `sessionStorage`.

## Consequences

- No hub code changed for the client's first release.
- A browser sign-in lasts as long as the keyring token it was handed: fifteen minutes. The
  client counts it down and asks again without losing the page.
- The face is Agent Robot Avatar by CX ArtLab (MIT), credited in the client; its character is
  theirs.
- Event names in the client are copied from `stream/events.py`. The client's
  `docs/protocol.md` names the hub file behind each one, and an event it has no reading for is
  counted on screen rather than dropped.

## What would change our minds

- **A `client` label on `CreateSession`** (or an author on `input.message`) would replace the
  title convention as the headed signal.
- **A hosted client** would need a `LUCY_UI_ORIGINS` CORS allow-list on the hub, decided here.
- **A longer-lived browser session** would be a keyring decision: a refresh grant scoped to the
  `lucy-api` audience, revocable by the person.
- **An AI SDK projection that carried the card's body** would let a stock `useChat` client
  replace most of the client's reducer.
