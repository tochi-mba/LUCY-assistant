# Private repositories and GitHub sign-in

Every repository in this family can be private. Three things need to read them, and each
signs in differently:

| Who | Signs in with | What it is used for |
| --- | --- | --- |
| You, on your machine | `gh auth login`: a browser window, or a pasted token | `git clone` in bootstrap, `uv`'s fetch of the client packages, `make images` |
| The devcontainer | the same login, forwarded as `GH_TOKEN`, or `gh auth login` inside it | the same |
| GitHub Actions | the **family GitHub App**, installed on the family repositories | a one-hour read-only token per CI job: `uv` fetches, the Docker build, the parity job's checkout of this repository |

Nothing else needs a GitHub credential. The running services never receive one.

## Sign in once

```bash
gh auth status              # which account is active, or "not logged in"
gh auth login               # gh asks: "Login with a web browser" or "Paste an authentication token"
bash scripts/bootstrap.sh   # runs that same login for you if you skipped it
```

Bootstrap runs `gh auth login` when you are not signed in and a terminal is available,
then `gh auth setup-git`, which makes `gh` the credential helper for `github.com`. From
then on `git clone`, `uv sync` (which fetches the client packages with git) and
`make images` (which hands `gh auth token` to the build) all use that account. A
repository your account cannot see is reported and skipped; nothing already cloned is
touched. `--dry-run` never signs in, configures git, or clones.

Without a terminal, such as a devcontainer's create hook or a script, set `GH_TOKEN` in
the environment; `gh` treats it as a login. The devcontainer forwards your host `GH_TOKEN`
and re-runs bootstrap each time a terminal attaches, so running `gh auth login` in that
terminal is enough.

## Connect CI: one command, two clicks

Actions cannot open a browser, and it must not hold your account's session. It gets a
GitHub App of its own, which can read the family repositories and nothing else.

```bash
uv run scripts/connect_github.py        # or: make github-ci
```

1. Your browser opens GitHub with the app already described. Click **Create GitHub App**.
2. GitHub sends you back and the script opens the app's install page. Choose **Only
   select repositories**, pick the nine (this repository and the eight in `repos.txt`),
   and click **Install**. If you miss one, the script names it and waits while you add
   it.

That is all. The script then stores the app's client id and private key as the Actions
secrets `FAMILY_APP_CLIENT_ID` and `FAMILY_APP_PRIVATE_KEY` on all nine repositories,
using your own `gh` login; removes the retired `FAMILY_GITHUB_TOKEN` secret where it finds
one; and triggers one CI run so you can watch it pass. The key goes from GitHub's reply
straight to `gh secret set` on standard input. It is never written to disk or printed.

`--dry-run` explains the plan and touches nothing. `--name` picks the app's name; GitHub
requires it to be unique, and the default is `<owner> family CI`.

Afterwards the app is listed under Settings → Developer settings → GitHub Apps, and its
installation under Settings → Applications → Installed GitHub Apps. To let CI read a
repository you add to the family later, add it there under *Repository access*. To
rotate the key, delete the app and run the command again.

### What CI does with it

Each job checks that the secrets are present, mints a token from the app with
`actions/create-github-app-token` (Contents: read, one hour, revoked when the job ends),
and tells git to use it for `github.com`, through git's configuration rather than the
log. The Docker job passes it as a BuildKit secret, mounted only while `uv sync` runs, so
it never lands in an image layer. The parity job checks out this repository with it and
does not persist it. Callers pass the secrets down with `secrets: inherit`.

Without the app, as on a fork, CI still runs: git fetches anonymously, which works while
the sources are public and fails with a clear message when they are not.

## Local image builds

```bash
make images    # here: GITHUB_TOKEN="$(gh auth token)" docker compose build
make docker    # in a service: the same, for that one image
```

Compose declares a build-time `github_token` secret sourced from `GITHUB_TOKEN`. It is a
build secret, not part of `.env.family`, and no running container sees it. Nothing
GitHub-related belongs in `.env.family`, a Docker `ARG`/`ENV`, or a committed file.

## Make the family private

Order matters: a public repository cannot call a private reusable workflow, so this
repository goes last.

1. Connect CI as above and let the triggered run finish green.
2. Make the **eight services** private. In the browser: repository → Settings → General →
   Danger zone → Change visibility. Or:

   ```bash
   gh repo edit OWNER/Keyring-api --visibility private --accept-visibility-change-consequences
   ```

   Re-run one consumer's CI; it now fetches private client tags with the app's token.
3. Make **LUCY-assistant** private, then allow its workflows to be used by your other
   repositories: Settings → Actions → General → Access → *Accessible from repositories
   owned by the user*. Or:

   ```bash
   gh api --method PUT repos/OWNER/LUCY-assistant/actions/permissions/access -f access_level=user
   ```

   Use `organization` instead of `user` for an organisation. Re-run one caller.

Private vulnerability reporting is a public-repository feature, so
[SECURITY.md](../SECURITY.md) gives an email address instead.

## Check the image boundary

`make test` covers the compose file, the workflow, the Makefiles, both bootstraps and the
connect script with fake tools and a fake GitHub, without touching your account. Two
opt-in checks use Docker:

```bash
LUCY_TEST_DOCKER=1 uv run --with pytest pytest tests/test_build_secrets.py -q          # a sentinel secret never reaches a layer
LUCY_TEST_FAMILY_IMAGES=1 uv run --with pytest pytest tests/test_image_runtime.py -q  # after make images
```

The string `x-access-token` appears in Dockerfile metadata by design; a leak would be the
token's value inside a layer, which the first test looks for byte by byte.
