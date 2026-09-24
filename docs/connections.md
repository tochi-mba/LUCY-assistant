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

## Capability gating

The music pack is the first real gated pack. Its seven operations appear in the model tool
registry only when the Spotify connection is active and carries the required scopes. Read
results are projected to track, artist, album and device metadata. Playback operations are
declared writes. Connecting Spotify makes those tools available on the next registry build;
disconnecting removes them again. Probe answers are cached per person, profile and pack
for fifteen seconds so a conversation that never mentions music does not wait on a devices
list every turn. A connect, a disconnect, a settings write, or a 502 that names a missing
credential drops the cache. Outbound calls to one provider for one person are serialised:
two turns refreshing the same grant at once are indistinguishable from replay, and RFC 9700
tells the authorization server to revoke the chain.
