# Connections and consent

Lucy exposes connection metadata, not provider credentials. Keyring remains the credential
vault and is the only service that stores access and refresh tokens.

## Boundary

Every connection request has two independent proofs:

- `Authorization: Bearer <lucy service token>` proves which service is calling Keyring.
- `X-Keyring-User-Token: <aud=lucy-api JWT>` proves which person is acting.

Keyring derives the account exclusively from the verified user token. A profile or service
name never selects an account, and another person's missing or existing resource is the same
404. Lucy's caller token is never forwarded as a sibling's bearer token.

## HTTP flow

1. `POST /v1/connections/{service}/authorize` asks Keyring to start provider consent.
2. Lucy stores only a short-lived opaque ticket and provider URL, bound to the verified
   account, profile and service.
3. The client opens the returned `/connect?ticket=...` URL on Lucy's origin.
4. Lucy verifies the browser subject again, consumes the ticket once, and redirects with
   `303 See Other` to the provider URL.
5. The client polls
   `GET /v1/connections/{service}/authorize/{ticket}`. While Keyring has no connection or
   only its `pending` placeholder, the state is `authorization_pending`; afterward it is
   Keyring's state.

Foreign, expired and unknown tickets are all 404. Reusing an opened ticket is 409. Tickets
contain no token or credential and expired records are pruned during normal use.

`GET /v1/connections` and `GET /v1/connections/{service}` return only service, state,
granted scopes, expiry and a credential-free last error. `DELETE` is idempotent.

## Music is a contract, not a service

The music capability speaks one HTTP contract: `POST /v1/lookup`, `GET /v1/player`,
`GET /v1/player/devices`, `GET /v1/player/recently-played`, and `POST /v1/player/play`,
`/queue` and `/pause`. Play and pause answer with the player state in which the command was
seen to take effect, or `504 confirmation-timeout`, carrying the last state seen, when it was
not; the model is then told the command was accepted but not confirmed, and to read
`music.nowPlaying` before sending it again. A queue is not confirmed: queueing does not
change anything the player state reports, so the service answers with the player state it
reads once the provider has accepted the command, and the hub reports each track as queued
on that acceptance. (A `504` on a queue would still be read as accepted and unconfirmed.)

Spotify-api is the family's implementation and the reference for the shapes
(`lucy_api.clients.music` names every field the hub reads). Any implementation of that contract can stand behind the capability: point
`LUCY_MUSIC_API_BASE_URL` at it and set `LUCY_MUSIC_API_AUDIENCE` to its own name. The
audience is the service's, not the contract's: keyring reads an audience as one service's
name and refuses a credential read whose token was minted for anybody else, so two
implementations never share one. A private implementation attaches the way every private
service does: a gitignored `docker-compose.local.yml` sets the two variables, its audience
is added to Lucy's allowlist under `exchange_audiences` in the gitignored
`scripts/genenv.local.json`, and it is never named in a public repository (ADR-0011).

## Repositories are a contract

The `repos` capability speaks the contract `lucy_api.clients.repos` names, served by
Github-api, the same way: point `LUCY_REPOS_API_BASE_URL` at an implementation and set
`LUCY_REPOS_API_AUDIENCE` to its name. A person connects GitHub through keyring provider
`github` (the Lucy GitHub App) or stores a fine-grained token with keyring's api-key route;
the probe is `GET /v1/me`, read like music's. Choosing which repositories and how much access
happens on GitHub; choosing what Lucy may do without asking happens in the permission gate.
[docs/repos.md](repos.md) has both.

## Capability gating

Music is gated by a probe, not by keyring's connection record and not by scopes. At the top
of a turn the probe asks the music service for this person's devices under the turn's
profile, with a token minted for the music audience:

- a `502` naming a missing credential (`credential-unavailable` or `credential-missing`)
  makes music `not_connected`, and the person is offered the connect link;
- a token the hub cannot mint, a service it cannot reach, or any other error makes music
  `unavailable`;
- any device list, an empty one included, makes music `ready`.

The hub checks no scopes. A grant that lacks one a command needs is refused by the service
when that command runs, and the refusal reaches the model as that step's error, or for a
queue as that track's reason.

Only a ready music capability puts its seven operations in the model's tool registry, and
then only when the person has not turned music off in settings and, once more than six
capabilities are ready, when music is one of the four kept bound (most recently used first;
on a fresh session music is not among them) or the model binds it with `capabilities.use`.
Read results are projected to track, artist, album and device metadata. Play, queue and
pause are declared writes, covered by the `music.control` permission, which asks before any
of them runs unless the person already allowed it.

Probe answers are cached per person, profile and capability for fifteen seconds, so a
conversation that never mentions music does not wait on a devices list every turn. Starting
a connection, opening its link, a poll that sees it settle, a disconnect, a settings write,
or a `502` naming a missing credential drops the cache, so connecting or disconnecting shows
on the next turn. Outbound calls for one person and profile to one service audience are
serialised: two turns refreshing the same grant at once are indistinguishable from replay,
and RFC 9700 tells the authorization server to revoke the chain.
