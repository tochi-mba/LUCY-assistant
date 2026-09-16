# ADR-0006: the family may be private

**Status:** accepted

## Context

Anonymous clones and tagged client fetches made public visibility a requirement.
The family needs to work under one private owner, including a private copy whose
repository names and client tags match the originals.

## Decision

All eight services and the meta repository may be private. The manifest remains
`<folder> <https clone URL>`; adding a repository is one line. Four mechanisms supply
credentials without putting a token in a project file:

| Caller | Authentication |
| --- | --- |
| Developer | `gh auth login --web`, then `gh auth setup-git` for git and uv |
| Devcontainer | the same browser login, or `gh auth login --web` inside the container |
| GitHub Actions | `scripts/share_github.py` copies that login into `FAMILY_GITHUB_TOKEN` |
| Local Docker build | `gh auth token` passed as a BuildKit secret by Make / Compose |

The private reusable workflow explicitly allows access from repositories under the
same owner. Docker mounts the secret only for `uv sync`; git reads a process-scoped
configuration and leaves no credential in a layer. Empty credentials permit anonymous
fetches when all referenced repositories are public.

`scripts/retarget.py OWNER` rewrites clone URLs, workflow references, the parity checkout,
and uv source URLs. It never rewrites lockfiles; the developer runs the printed relock
commands while signed in to their copy. `--keep-sources` supports retaining upstream
client tags where access is available.

## Consequences

The owner signs in through the browser. CI cannot; `share_github.py` is the
handoff. Adding a repository is a manifest line plus re-running that command.
A public caller cannot use a private reusable workflow, so service visibility
changes precede the meta repository's change. The rollout order is documented
in [private-repos.md](../private-repos.md).

## What would change our minds

A GitHub App installation token would rotate credentials per job without copying
a user session into Actions, at the cost of creating and installing an app.
Publishing the clients could remove git authentication for packages, while clones
and private CI reuse would still need a GitHub login.
