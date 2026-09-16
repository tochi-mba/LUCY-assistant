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
| GitHub Actions | the family **GitHub App**, installed on the nine repositories with Contents read-only; each job mints a one-hour token from it | `scripts/connect_github.py` creates and installs it from the browser and stores its client id and key as secrets; callers `secrets: inherit` |
| A local image build | the developer's `gh auth token`, as a BuildKit secret | `make images` / `make docker` |

In CI the token reaches git through `url.insteadOf` configuration before each `uv sync`,
the Docker build through a `--mount=type=secret` scoped to the `uv sync` RUN, and the
parity job's checkout through `token:` with `persist-credentials: false`. Without the app,
the credential is empty and git fetches anonymously, so a public fork still builds. Two
parity checks, `ci-secrets` and `docker-secret`, hold every service to this shape.

CI does not get a developer's `gh` session. That session carries the `repo` and
`workflow` scopes, which is write access to every repository on the account, and a
secret is readable by any workflow in the repository that holds it. It does not get a
personal access token either: one has to be assembled by hand in GitHub's settings, it
expires, and it is one long-lived value copied into nine places. An App is created by
GitHub from a manifest the script posts, so the person only clicks; its installation is
the list of repositories it can read; the tokens CI uses last an hour and are revoked
when the job ends; and the only long-lived secret, the app's key, can do nothing but
mint those read-only tokens.

`scripts/retarget.py OWNER` rewrites the owner in a copy's clone URLs, workflow
references, parity checkout and uv source URLs. It never rewrites lockfiles; it prints
the `uv lock` commands to run. A copy connects its own app the same way.

## Consequences

A developer signs in once. The owner runs one command and clicks twice. Adding a
repository is a manifest line plus adding it to the app's installation. Service
repositories go private before this one, because a public caller cannot use a private
reusable workflow, and this repository then has to allow access from the owner's other
repositories. Private vulnerability reporting is unavailable, so disclosure is by email.

## What would change our minds

Publishing the clients to a package index would take git authentication out of
`uv sync`, though clones and the private reusable workflow would still need a login.
