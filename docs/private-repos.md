# Private repositories and GitHub sign-in

All nine repositories can be private. These credentials read source code; application
service tokens in `.env.family` serve a different purpose.

| Where you are working | Which sign-in to use | What reads it |
| --- | --- | --- |
| Developer machine | `gh auth login` by browser or pasted token, then `gh auth setup-git` | git clone and uv's git fetches |
| Non-interactive shell or devcontainer | `GH_TOKEN` from the host environment / secret manager | GitHub CLI's git credential helper |
| GitHub Actions | Repository secret `FAMILY_GITHUB_TOKEN` | git during uv fetches, parity's meta checkout, Docker BuildKit |
| Local image build | `make images` or the service's `make docker`, using `gh auth token` | BuildKit's temporary `github_token` mount |

Bootstrap configures the helper once per machine and reports the active GitHub login.
Without a terminal it explains how to sign in and continues with accessible checkouts.
The devcontainer forwards `GH_TOKEN`; you can instead sign in from its terminal and
re-run bootstrap. `--dry-run` never signs in, configures credentials, or clones.

## Create the CI token

The owner creates a **fine-grained personal access token** in GitHub Settings → Developer
settings → Personal access tokens → Fine-grained tokens. Use resource owner `tochi-mba`
(your owner for a copy), a one-year expiry, and **Only select repositories**:

`LUCY-assistant`, `Keyring-api`, `Settings-api`, `User-api`, `Persona-api`, `Media-tool`,
`Web-search-api`, `Spotify-api`, `Environments-api`.

Grant **Contents: Read-only**; **Metadata: Read-only** is automatic. Grant no write or
account permissions. The token reads source; your existing owner sign-in administers
secrets and repository settings. See [GitHub's token guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

Set `FAMILY_GITHUB_TOKEN` as an Actions repository secret in every repository. The CLI
accepts a hidden prompt, so a one-repository operation is:

```bash
gh secret set FAMILY_GITHUB_TOKEN --repo tochi-mba/Keyring-api
```

For all nine, from Git Bash/Linux/macOS in the meta root, paste once into a hidden prompt
and pass the value to `gh` on standard input. This avoids putting it in process arguments:

```bash
read -rsp 'Family read-only token: ' FAMILY_GITHUB_TOKEN; printf '\n'
for repo in LUCY-assistant $(awk '!/^#/ && NF {print $1}' repos.txt); do
  printf '%s' "$FAMILY_GITHUB_TOKEN" |
    gh secret set FAMILY_GITHUB_TOKEN --repo "tochi-mba/$repo" || break
done
unset FAMILY_GITHUB_TOKEN
```

Do not enable shell tracing. Never commit the token, store it in `.env.family`, or use a
Docker `ARG`/`ENV` for it. GitHub CLI normally uses the OS credential store for developer
sign-in; CI secrets live in GitHub. Runner git configuration lasts for that job. Docker
reads `/run/secrets/github_token` only during dependency installation and configures git
through process environment variables. The image has no GitHub credential.

## Rotate, revoke, and diagnose

Create the replacement with the same repository list and read permissions, replace all
nine Actions secrets, and rerun a consumer CI workflow before revoking the old token.
Update any externally managed `GH_TOKEN` that used the old token. Adding a repository
requires updating the selected repository list and installing the secret there too.

Use `gh auth status` to diagnose local access. A successful sign-in does not grant
repository membership; ask the owner for access when a clone or a pinned client tag is
unreadable. In CI, inspect the authentication step and check secret presence with
`gh secret list --repo OWNER/REPO`, which displays names, never values.

Without `FAMILY_GITHUB_TOKEN`, the authentication step does nothing and Docker's optional
mount is empty: public dependencies can still be fetched anonymously. Private client
fetches fail. Parity falls back to that job's `github.token`, which cannot read another
private repository. A public fork also needs a public reusable workflow (retarget to its
own meta copy); secrets are generally unavailable to pull requests from forks.

## Rollout without breaking callers

1. Merge the eight service changes first. `secrets: inherit` works with the previous
   `v1`; optional Docker mounts also work with public sources.
2. The owner creates and installs the token in all nine repositories as above.
3. Merge the meta changes. Move the `v1` workflow tag to the tested meta commit, then
   run one consumer CI workflow and confirm its authentication step and image build.
   Use `git tag -f v1 <tested-commit>` and `git push origin refs/tags/v1 --force` only
   for this documented moving workflow tag; client package tags remain immutable.
4. The owner makes the eight service repositories private, then reruns a consumer:
   `gh repo edit OWNER/REPO --visibility private --accept-visibility-change-consequences`.
5. The owner makes the meta repository private and immediately grants workflow access:

   ```bash
   gh api --method PUT repos/OWNER/LUCY-assistant/actions/permissions/access -f access_level=user
   ```

   `user` is for repositories in a personal account; use `organization` for an
   organization owner. Then rerun a caller. A public repository cannot call a private
   reusable workflow, which is why the meta visibility change comes last. See
   [GitHub's workflow access rules](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).

Token creation and visibility changes are owner actions. Do not advance the visibility
steps until the new workflow is running successfully with the installed token.

## Verify the image boundary

Run `make test`, `make parity`, both bootstrap dry runs, and each service's `make check`.
Then `python scripts/genenv.py` (once), `make images`, and `make up`. Probe ports
8001–8008 at `/ready`. Use `docker compose down` to stop without deleting data.

For a leak check, use a disposable sentinel credential and inspect image history,
config, and saved layers for its exact bytes. The literal string `x-access-token` can
occur in Dockerfile command metadata; its presence alone is not a credential leak.
BuildKit's [secret mounts](https://docs.docker.com/build/building/secrets/) keep the
mounted contents out of the resulting layers.

The executable regression test runs every service's dependency RUN with a checking
stand-in for uv and absent, empty, and sentinel secrets. It checks git's resolved URL,
the following build step, and exported image metadata and layers:

```bash
LUCY_TEST_DOCKER=1 uv run --with pytest pytest tests/test_build_secrets.py -q
```

Run it after bootstrap, with Docker Engine available. `make test` runs its archive
scanner unit tests and skips the three Docker integration cases unless opted in.
