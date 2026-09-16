# Security

This is the family threat model, not a substitute for each service's
`docs/security.md`. Read those before deploying that process. Environments-api
especially: it is remote code execution as a product.

## What we are protecting

- Credentials in keyring (OAuth grants, API keys, stored passwords).
- Personal data in user-api, persona-api, and settings-api (plaintext at rest).
- The ability of one account to read another.
- The ability of one service to act as another (confused deputy).
- Command execution leaking out of an environment.

## Rules that apply everywhere

**Bearer is identity.** A request is for the `sub` of a keyring-signed JWT
whose `aud` is this service. There is no header that names an account id, and
no query parameter that would let a caller pick a victim.
[ADR-0003](adr/0003-bearer-canonical.md).

**Local verification.** Consuming services do not ask keyring "is this token
good?" on every request. They fetch JWKS, cache it, and verify. An outage of
keyring degrades issuance, not verification of tokens already minted, until
the keys go stale.

**Two tokens on internal surfaces.** `/v1/internal` on keyring and on
settings-api requires the caller's service token *and* the user's JWT. The
service token names the caller; the JWT's `aud` must be that same name (or the
grant's `audience_prefix`). A stolen service token without a matching user
token reads nothing. A stolen user token without the service token never
reaches the internal surface.

**Unknown env vars fail startup.** A typo in `KEYRING_MASTER_KEY`'s name would
otherwise start a sealed vault that looks healthy enough to ship. Each service
refuses prefixed variables it does not recognise.

**Unauthenticated health endpoints report counts and yes/no, never a name.**
A counter that moves when one person acts is an oracle. Keyring already paid
for that lesson.

**No secret in a log, a traceback, or this meta-repo's script output.**
`genenv.py` writes `.env.family` and prints a count. Bootstrap never dumps
the environment. Do not `export KEYRING_*` in a ticket.

**Disclosure goes to the maintainer** through this public repository's private
vulnerability reporting or by email at the address in [SECURITY.md](../SECURITY.md).

**GitHub credentials are a sign-in, not a file.** A developer uses `gh auth login`;
`gh` keeps the session in the OS credential store and acts as git's credential
helper. That is also how someone runs Lucy on their own machine after cloning. CI uses
the public family GitHub App, installed on each owner's selected repositories with
Contents read-only. A Cloudflare broker holds the app key and accepts only GitHub-signed
OIDC identities from the canonical `service.yml@v1`; it returns a one-hour read-only
token for the repositories in the caller owner's installation. No user's repository receives
the app key or a long-lived personal token. Docker builds receive the result as a
BuildKit secret mounted for the `uv sync` RUN only. It is never a build argument, an
image environment variable, or a line in `.env.family`, which every running service
receives. See [private-repos.md](private-repos.md).

## What this meta-repo must not do

- It must not contain a service token, GitHub token, master key, or `.env.family`.
- It must not pull, reset, or checkout an existing service clone. Other people
  are working in those repositories.
- It must not vendor `keyring_client` or `settings_client`. They live in the hub.

## Environments-api

Needs Linux. User and namespace sandbox tiers need privileges. Compose sets
`privileged: true` for that service; a deployment that cannot offer that must
keep `ENVAPI_MIN_SANDBOX_TIER=directory` and accept the weaker isolation, or
not run it.
