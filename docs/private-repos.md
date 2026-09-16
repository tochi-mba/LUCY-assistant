# Private repositories and GitHub sign-in

All nine repositories can be private. You sign in to GitHub **in the browser**.
There is no personal access token to mint.

Application tokens in `.env.family` are unrelated: those are keyring and
settings-api service tokens.

Day to day:

```bash
gh auth login --web          # only if `gh auth status` says you are not signed in
bash scripts/bootstrap.sh
make images                  # uses the same browser session
```

If you ever log out of `gh`, sign in in the browser again, then run
`python scripts/share_github.py` (or `make github-ci`) so Actions keeps the same login.

| Where you are working | How you sign in | What reads it |
| --- | --- | --- |
| Developer machine | `gh auth login --web` (browser window), then `gh auth setup-git` | git clone and uv's git fetches |
| Devcontainer | the same browser login on the host, or `gh auth login --web` inside the container | GitHub CLI's git credential helper |
| Local image build | `make images` / `make docker`, which call `gh auth token` from that login | BuildKit's temporary `github_token` mount |
| GitHub Actions | `python scripts/share_github.py` copies the browser login into the `FAMILY_GITHUB_TOKEN` repository secret | git during uv fetches, parity's meta checkout, Docker BuildKit |

Actions cannot open a browser. That is the only reason a repository secret exists.
It is your existing GitHub CLI session, not a separately created PAT. Re-run
`share_github.py` after `gh auth login --web` if you ever log out of `gh`.

Bootstrap opens the browser when you are not signed in. `--dry-run` never signs
in, configures credentials, or clones. Never put `GH_TOKEN`, `GITHUB_TOKEN`, or
`FAMILY_GITHUB_TOKEN` in `.env.family`, a Docker `ARG`/`ENV`, or a committed file.

## Give Actions the same login

From the family root, already signed in via the browser:

```bash
python scripts/share_github.py --dry-run
python scripts/share_github.py
```

That sets `FAMILY_GITHUB_TOKEN` on `LUCY-assistant` and every repository in
`repos.txt`. The credential travels on standard input to `gh secret set`. It is
not printed and not placed in process arguments. Adding a repository is: append
a line to `repos.txt`, then run the command again.

`gh secret list --repo OWNER/REPO` shows names, never values. `gh auth status`
shows which account is signed in locally.

Without the secret, CI still runs while the family is public (anonymous git
fetches). Private client tags fail until you run `share_github.py`. Parity's
meta checkout falls back to that job's `github.token`, which cannot read another
private repository.

## Make the family private

Do this in the browser if you prefer: each repository → Settings → General →
Danger zone → Change repository visibility → Private. Order still matters.

1. Run `python scripts/share_github.py` and confirm one consumer CI workflow
   fetched its client packages.
2. Make the **eight services** private.
3. Make **LUCY-assistant** private last, then allow the reusable workflow to be
   used by your other repositories (Settings → Actions → General → Access →
   repositories in this account). The equivalent CLI, using the same browser
   login, is:

   ```bash
   gh api --method PUT repos/OWNER/LUCY-assistant/actions/permissions/access -f access_level=user
   ```

A public repository cannot call a private reusable workflow, which is why the
meta repository goes last. See [GitHub's workflow access rules](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).

`user` is for a personal account; use `organization` for an organization owner.

## Verify the image boundary

Run `make test`, `make parity`, both bootstrap dry runs, and each service's
`make check`. Then `python scripts/genenv.py` (once), `make images`, and
`make up`. Probe ports 8001–8008 at `/ready`. `docker compose down` stops
without deleting data.

For a leak check, use a disposable sentinel credential and inspect image
history, config, and saved layers for its exact bytes. The literal string
`x-access-token` can occur in Dockerfile command metadata; its presence
alone is not a credential leak. BuildKit's
[secret mounts](https://docs.docker.com/build/building/secrets/) keep the
mounted contents out of the resulting layers.

```bash
LUCY_TEST_DOCKER=1 uv run --with pytest pytest tests/test_build_secrets.py -q
```
