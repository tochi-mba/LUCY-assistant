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
| Developer | `gh auth login`, then `gh auth setup-git` for git and uv |
| Non-interactive shell / devcontainer | `GH_TOKEN` supplied in the environment |
| GitHub Actions | `FAMILY_GITHUB_TOKEN`, Contents read-only on the nine repositories, inherited by service callers |
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

The owner maintains one fine-grained token, rotates its nine Actions secrets, and grants
new repositories access explicitly. A public caller cannot use a private reusable
workflow, so service visibility changes precede the meta repository's change.
Visibility and token creation remain owner actions. The rollout order and permissions
are documented in [private-repos.md](../private-repos.md).

## What would change our minds

A GitHub App installation token could replace the personal token if the family grows
beyond a single owner or needs automated rotation. Publishing the clients could remove
git authentication for packages, while clones and private CI reuse would still need it.
