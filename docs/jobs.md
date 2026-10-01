# Jobs and signals: work that outlives a turn

"Run the tests and tell me when they finish." "Let me know when CI is green, then merge it."
"Wake me when the export lands." Each of these is a piece of work that keeps going after the
reply, and ends at a moment nobody chose. This page is the one way the family does it, for
every kind of work and every service.

## One registry, four kinds, one ending

Everything that outlives a step is a record in the hub's work registry
(`src/lucy_api/work/registry.py`). Starting it returns a **handle** at once; the turn carries
on. When it ends, the registry does the same three things for every kind:

1. a one-line **notice** at the model's next tool boundary, and a line in the live block;
2. a `lucy.work.finished` **event** for clients;
3. if the work asked to **wake** the session and nobody is talking, a **turn is opened** with a
   harness notice as its input (`work/wake.py`), so Lucy can tell the person and carry on.

The result is never pushed: `work.result` fetches it, framed as untrusted. `work.check`,
`work.wait` and `work.cancel` work on every kind alike.

| Kind | What runs it | Lives | Example |
| --- | --- | --- | --- |
| `helper` | a child of the turn loop (`agents/runtime.py`) | the process; continued after a restart | "review both branches" |
| `command` | the session's sandbox | the process | `workspace.run` with `wait: false` |
| `watch` | the hub, polling every 5–300 s | at most an hour, in the process | `watch.start` on a file or a public URL |
| **`subscription`** | **the sibling that can see the condition** | **up to a week, durable** | `repos.watch` until CI settles |

Pick a **watch** when only Lucy can look, for a few minutes. Pick a **subscription** whenever a
service can see the condition itself -- it is cheaper (no polling through the hub), it
survives a restart, and it can act for the person when it ends.

## Subscriptions

A subscription is a record of kind `subscription` *and* a row in the hub's `subscriptions`
table (`work/subscriptions.py`). The flow, for a capability whose sibling implements the
contract below:

```
 turn (person present)                    sibling                      hub, later
 ─────────────────────                    ───────                      ──────────
 context.subscriptions.open(...) ──► row + record + secret
   [wake: true → standing consent recorded in keyring]
 POST /v1/subscriptions {signal: {url, secret}} ──►  looks, for as long as it lives
 context.subscriptions.attach(opened, sibling_id)
 "Watching. I'll tell you."                        condition holds
                                                    POST /v1/signals/{id}  ──► verify, end the record
                                                    (signed with secret)        registry: notice, event, wake
                                                                                woken turn acts under consent
```

### Opening one, from a capability

A pack never touches tokens or rows. Its `PackContext` carries a per-turn seam:

```python
opened = await run.ctx.subscriptions.open(
    capability="repos",
    objective="Say when CI on tochi-mba/LUCY-assistant#42 is green",
    timeout_seconds=3600,
    wake=True,
)
try:
    sibling = await client.subscribe(..., signal_url=opened.signal_url, secret=opened.secret)
except DownstreamError:
    run.ctx.subscriptions.abandon(opened)   # ends cancelled; nothing is left behind
    raise
await run.ctx.subscriptions.attach(opened, sibling.id)
return {"id": opened.handle.id, "state": "running"}
```

and registers, once, how its sibling is released on a cancel and asked during a sweep:

```python
subscriptions.on_release("repos", release)   # DELETE the sibling's subscription
subscriptions.on_check("repos", check)       # GET it; a Signal when it has ended
```

The operation that opens a waking subscription is a **write**, declared under a permission
whose description says what Lucy will be able to do when it ends -- "keep watching a
repository and act on it when it changes". It is asked about like any other write; `auto`
may run it; a stored allow skips the card.

### Standing consent: how a woken turn acts

When `wake` is true the seam records **standing consent** before the subscription opens: an
offline grant in keyring, for this hub, for the subscription's lifetime plus fifteen minutes
(`core/standing.py`). It is made with the person's own token while they are present, through
keyring's `POST /v1/internal/profiles/{name}/grants`, and the person sees it in their list of
grants and can revoke it there.

When the subscription ends and the session is idle, the waker asks for that consent back:
Lucy exchanges the grant for a token for itself, verifies it as it verifies any caller, and
prepares the woken turn exactly as it prepares a turn a person sent -- their settings, their
permission mode, their grants, the same gate. The harness notice tells the model whether it
has consent ("act on what they asked for, and nothing more") or not ("say what happened and
ask them"). Consent that was revoked, expired or cannot be reached makes a turn without
authority, never a crash.

A cancelled subscription withdraws its consent, using the grant itself to give itself up. One
that ends normally lets it expire, because the turn it opens is still using it.

### Restarts, lost signals, cancels

- **Restart.** The registry does not announce a subscription as stopped. On startup the hub
  re-registers every open row under its original work id, with what is left of its lifetime;
  one already past its deadline ends `timed_out` at once and is told like any expiry.
- **Lost signal.** Every `LUCY_SUBSCRIPTION_SWEEP_SECONDS` (120) the hub asks each open
  subscription's sibling whether it ended (`on_check`). A sweep and a signal that race end the
  row once.
- **Cancel.** `work.cancel` ends the record `cancelled`; the capability's `on_release` and the
  consent withdrawal run, best effort, and a late signal is a 409.

## The sibling side of the contract

A service that runs work for Lucy past a request implements three routes, authenticated like
every other person-scoped route (a minted token for its audience, `X-Keyring-Profile`):

| Route | Body / answer |
| --- | --- |
| `POST /v1/subscriptions` | `{kind, target, expires_at, signal: {url, secret}}` → `201 {id, state}`. Refuse a `kind` you do not know with a 422 naming the ones you do. |
| `GET /v1/subscriptions/{id}` | `{state: running\|fired\|failed\|expired, summary?, facts?, excerpt?}`. Another account's id is 404. |
| `DELETE /v1/subscriptions/{id}` | `204`, idempotent. Stop looking. |

When the condition holds -- or cannot, or the subscription expires -- POST **one signal** to
`signal.url`:

```
POST {signal.url}
Content-Type: application/json
X-Lucy-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed with signal.secret>

{"state": "fired", "summary": "CI on #42 is green", "facts": {"conclusion": "success"}, "excerpt": "..."}
```

- `state` is `fired`, `failed` or `expired`; `summary` is one line (120 characters kept);
  `facts` is at most twelve scalars; `excerpt` is at most 1,500 characters. The body is at most
  8 KiB. It says **that** it happened, never the result: the woken turn reads that through the
  capability.
- `204` means heard; `409` means it had already ended -- an earlier try was heard. Retry a
  refused connection or a 5xx three times over about two minutes, then stop: the sweep covers
  the rest. Any other 4xx will be refused again; do not retry it.
- Hold the secret with the subscription and nowhere else. Never log it, never return it.
- Cap every lifetime at what you can honour, and expire on your own: a subscription the hub
  forgot (it was cancelled while you were unreachable) must not run for ever.

`clients/python/lucy_signals` (tag `lucy-signals-v<version>`) is that POST, done one way:
`Signal`, `deliver(client, url=..., secret=..., signal=...)`, `verify_signature`. A test in
`tests/hub/test_lucy_signals.py` holds its signatures to the hub's byte for byte.

## Where this lives

| | |
| --- | --- |
| `work/registry.py` | every kind's handle, notice, cancel, timeout and restart rule |
| `work/wake.py` | opening a turn on an idle session, and the consent line in its notice |
| `work/subscriptions.py` | rows, signals, restore, sweep, release, and the per-turn seam |
| `core/standing.py` | recording, using and withdrawing standing consent |
| `api/routers/signals.py` | `POST /v1/signals/{id}` |
| `net/signing.py` | the one signature scheme, for webhooks out and signals in |
| `clients/python/lucy_signals` | the sibling's side |

The decision and its alternatives are [ADR-0015](adr/0015-jobs-and-signals.md).
