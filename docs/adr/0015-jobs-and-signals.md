# ADR-0015: work a sibling finishes is a durable subscription, ended by a signed signal

**Status:** accepted (2026-10-01)

## Context

The hub already had one shape for work that outlives a step: the work registry, with a
handle, a notice, a live-block line and -- when asked -- a turn opened on an idle session.
Helpers, sandbox commands and watches all use it, and the shape is right.

Three things it could not do, and "tell me when CI is green, then merge it" needs all three:

1. **Survive a restart.** Records live in memory. A watch is gone the moment the hub
   restarts, and the person is never told.
2. **Be told, rather than look.** A watch polls from the hub, at most hourly-long, every few
   seconds, through the sibling. The sibling that can see CI could simply say when.
3. **Act when it ends.** A woken turn had no prepared context and so no way to reach a
   sibling for the person: it could announce that CI finished but not merge.

Every service with long work also spoke its own dialect for it (`?async=true` and a jobs
route here, a command handle there), so the hub learned each one separately.

## Decision

**A new kind, `subscription`, in the same registry.** It has every property the other kinds
have -- handle, notice, event, wake, `work.check`/`wait`/`cancel`/`result` -- and two more: a
durable row (`subscriptions` table) and an ending that arrives as a signal.

**Siblings end subscriptions with a signed signal**, `POST /v1/signals/{id}`, HMAC-SHA256 over
the raw body with a per-subscription secret, in the header Lucy's own webhooks already use
(`X-Lucy-Signature`). Unknown ids and bad signatures are the same 404; an ended subscription is
409. The signal says that the condition held -- a summary, a few small facts, a bounded
excerpt -- and never the result, which the woken turn reads through the capability, framed as
untrusted.

**Durability is the row plus two recoveries.** On startup the hub re-registers every open row
under its original work id. Every two minutes a sweep asks each open subscription's sibling
whether it ended, for the signal that was lost. A restart is not announced as an ending.

**A woken turn acts under standing consent, never a held token.** When a subscription will
wake, the turn that opens it records an offline grant in keyring for this hub, for the
subscription's life plus fifteen minutes, with the person's own token while they are present
(keyring gains `POST /v1/internal/profiles/{name}/grants` for this). When it ends, the hub
exchanges the grant for a token for itself, verifies it as it verifies any caller, and
prepares the woken turn through the unchanged `prepare_turn`. A cancel withdraws the grant
under the grant itself. Unusable consent makes a turn told it has none.

**One contract for siblings**, documented in [jobs.md](../jobs.md): three routes
(`POST/GET/DELETE /v1/subscriptions`) and one signal. The sending side is a small client,
`clients/python/lucy_signals`, owned here like the other shared clients (ADR-0002), with a
test that holds its signatures to the hub's byte for byte.

## Consequences

- Lucy's database gains a `subscriptions` table; a session's deletion cascades to its rows.
- `LUCY_SIGNAL_BASE_URL` names the hub as siblings reach it, which in compose is not the
  address a person types. `LUCY_SUBSCRIPTION_SWEEP_SECONDS` sets the sweep.
- `lucy-api` joins its own exchange allowlist (`LUCY_EXCHANGE_AUDIENCES`): minting a token for
  itself under a grant is how a woken turn becomes a person's turn.
- The secret is stored as the webhook secrets are, in the hub's database: an HMAC key has to
  be held to check a signature. It authorises ending one subscription and nothing else.
- The registry gained a fix found on the way: work cancelled before its task first ran was
  never recorded as ended. Opening a subscription the sibling then refuses does exactly that.
- Watches remain, for conditions only the hub can see (a workspace file, a public URL).

## Alternatives rejected

- **Keep polling from the hub, but persist watches.** Durable, but the hub still does the
  sibling's looking, through the sibling, every few seconds, for a week.
- **Hold the person's token until the work ends.** A credential in memory for an hour, lost on
  restart, and invisible to the person. A grant is visible, revocable and survives.
- **A woken turn that may only speak.** True to "nothing acts without the person", and useless
  for the request that motivated this: "merge it when CI is green" is consent given in
  advance, recorded as such.
- **Webhooks from GitHub straight to the hub.** The hub would learn GitHub's event model,
  violating the rule that the hub knows capabilities, not services. The sibling may receive
  provider webhooks itself; what crosses to the hub is always a signal.

## What would change our minds

A family-wide event bus with delivery guarantees would make the sweep unnecessary. A need to
act on the person's behalf for longer than a week would want a different consent screen, not a
longer subscription.
