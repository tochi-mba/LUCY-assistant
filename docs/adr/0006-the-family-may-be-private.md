# ADR-0006: the family may be private

**Status:** accepted

## Context

The family only worked as public repositories. Bootstrap cloned anonymously; every
`uv sync` fetched the two client packages from GitHub anonymously, locally, in CI and
inside Docker builds; each service's CI called the reusable workflow in this repository,
and GitHub forbids a public repository calling a private one; the devcontainer cloned
before anyone could sign in. The owner wants the opposite: everything private, one sign-in
for a developer, one documented line to add a repository, and a private copy of the whole
family that keeps working.

## Decision

All nine repositories may be private. `repos.txt` keeps its `<folder> <https clone URL>`
format. Four readers, four credentials, none of them in a project file:

| Reader | Credential | Configured by |
| --- | --- | --- |
| A developer | `gh auth login` (browser or pasted token), then `gh auth setup-git` makes `gh` git's credential helper | `scripts/bootstrap.sh` / `.ps1`, once |
| A shell without a terminal; the devcontainer | `GH_TOKEN` in the environment | `devcontainer.json` `remoteEnv` |
| GitHub Actions | `FAMILY_GITHUB_TOKEN`: a fine-grained token, Contents read-only, only the family repositories | `scripts/share_github.py` sets it on all nine; callers `secrets: inherit` |
| A local image build | the developer's `gh auth token`, as a BuildKit secret | `make images` / `make docker` |

In CI the token reaches git through `url.insteadOf` configuration before each `uv sync`,
the Docker build through a `--mount=type=secret` scoped to the `uv sync` RUN, and the
parity job's checkout through `token:` with `persist-credentials: false`. An empty
credential falls back to an anonymous fetch, so a public fork without the secret still
builds. Two parity checks, `ci-secrets` and `docker-secret`, hold every service to this
shape.

CI does not get a developer's `gh` session. That session carries the `repo` and
`workflow` scopes, which is write access to every repository on the account, and a
secret is readable by any workflow in the repository that holds it. The fine-grained
token can read nine repositories and nothing else, and it expires.

`scripts/retarget.py OWNER` rewrites the owner in a copy's clone URLs, workflow
references, parity checkout and uv source URLs. It never rewrites lockfiles; it prints
the `uv lock` commands to run.

## Consequences

A developer signs in once. The owner creates one token, installs it with one command and
rotates it the same way before it expires. Adding a repository is a manifest line plus
adding it to the token's repository list. Service repositories go private before this
one, because a public caller cannot use a private reusable workflow, and this repository
then has to allow access from the owner's other repositories. Private vulnerability
reporting is unavailable, so disclosure is by email.

## What would change our minds

A GitHub App installation would mint a short-lived token per job and remove the expiry
chore, at the cost of creating and installing an app. Publishing the clients to a package
index would take git authentication out of `uv sync`, though clones and the private
reusable workflow would still need a login.
