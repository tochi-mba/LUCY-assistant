# ADR-0006: the family may be private

**Status:** accepted

## Context

The family only worked as public repositories. Bootstrap cloned anonymously; every
`uv sync` fetched the two client packages from GitHub anonymously, locally, in CI and
inside Docker builds; each service's CI called the reusable workflow in this repository,
and GitHub forbids a public repository calling a private one; the devcontainer cloned
before anyone could sign in. The owner wants the opposite: everything private, one sign-in
for a developer, nothing to create by hand in GitHub's settings, one documented line to
add a repository, and a private copy of the whole family that keeps working.

## Decision

All nine repositories may be private. `repos.txt` keeps its `<folder> <https clone URL>`
format. Four readers, four credentials, none of them in a project file:

| Reader | Credential | Configured by |
| --- | --- | --- |
| A developer | `gh auth login` (browser or pasted token), then `gh auth setup-git` makes `gh` git's credential helper | `scripts/bootstrap.sh` / `.ps1`, once |
| A shell without a terminal; the devcontainer | `GH_TOKEN` in the environment | `devcontainer.json` `remoteEnv` |
| GitHub Actions | the public **lucy-assistant family CI** app, installed on the family repositories with Contents read-only; each job exchanges GitHub OIDC proof for a one-hour token | `scripts/connect_github.py` opens the Install page; the Cloudflare broker owns the app key |
| A local image build | the developer's `gh auth token`, as a BuildKit secret | `make images` / `make docker` |

In CI the caller grants `id-token: write`. The broker verifies GitHub's OIDC signature,
issuer, audience, expiry, repository owner, and canonical reusable-workflow identity,
then mints a token scoped to repositories in that owner's app installation. The token
reaches git through `url.insteadOf` configuration before each `uv sync`,
the Docker build through a `--mount=type=secret` scoped to the `uv sync` RUN, and the
parity job's checkout through `token:` with `persist-credentials: false`. Without the app,
the broker is unavailable CI fails closed. Two parity checks, `ci-secrets` and
`docker-secret`, hold every service to this shape.

CI does not get a developer's `gh` session. That session carries the `repo` and
`workflow` scopes, which is write access to every repository on the account, and a
secret is readable by any workflow in the repository that holds it. It does not get a
personal access token either: one has to be assembled by hand in GitHub's settings, it
expires, and it is one long-lived value copied into nine places. The public app can be
installed by any GitHub owner, but installing it does not disclose its private key.
That key exists only as a Cloudflare Worker secret. The broker can mint only read-only,
one-hour installation tokens, and only after a job running the canonical public family
workflow presents a valid GitHub OIDC identity. `--create-app` remains for an operator
that also runs an isolated broker.

`scripts/retarget.py OWNER` rewrites the owner in a copy's clone URLs and uv source
URLs. It deliberately keeps callers on the canonical public workflow because the
broker verifies its OIDC identity. It never rewrites lockfiles; it prints the `uv lock`
commands to run. The copy then installs the same public app on that owner's repositories.
Running Lucy on a laptop never needs the app; `gh auth login` is the credential.

## Consequences

A developer signs in once. Connecting CI is one Install click on the public app. Adding
a repository is a manifest line plus adding it to the app's installation. Copies and
service repositories may be private. The canonical meta repository stays public so
private repositories under any owner can call its audited workflow and action. It
contains no service code or credentials. Private vulnerability reporting remains
available on that canonical public repository.

## What would change our minds

Publishing the clients to a package index would take git authentication out of
`uv sync`, though clones and the private reusable workflow would still need a login.
