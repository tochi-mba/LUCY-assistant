# ADR-0003: Bearer is canonical

**Status:** accepted

## Context

The eight services grew up at different times. Some already used
`Authorization: Bearer`. Some documented an API key. Some accepted both. An
assistant sitting in front of all eight cannot be asked to remember which.

## Decision

The identity of a **person** on any family HTTP surface is:

```
Authorization: Bearer <token>
```

The token is a keyring-signed JWT (except on keyring itself, where a person holds
an opaque session, still presented as Bearer). `aud` is the service being called.
There is no account-id parameter.

A static API key, where it still exists (Web-search-api, Environments-api), is a
**network-level gate**, not identity. It does not name a person and does not
replace the JWT.

## Why

**One header the MCP layer can set.** Anything else becomes a per-service adapter
in the one place we most want to keep boring.

**Confused-deputy resistance depends on `aud`.** A token minted for one service
must not work on user-api. Bearer JWTs carry that claim; API keys do not.

**Logs and middleware already know this shape.** Request-id, authn, and
error-redaction all key off the same header.

## What it costs

A few checkouts still mention API keys in `.env.example`. That is compatibility,
scheduled to become optional-off, not a second identity system. Health endpoints
remain unauthenticated because a load balancer cannot hold a token.

## What would change our minds

An MCP OAuth 2.1 authorization-server flow in front of keyring would still present
as Bearer to the family. A different header would mean every service, every
client, and every piece of middleware changes at once — which is the cost that
makes this sticky.
