# Private repositories and GitHub sign-in

The canonical published family repositories are public. Your own copy may make any
repository private. Three things need to read private repositories, and each signs
in differently:

| Who | Signs in with | What it is used for |
| --- | --- | --- |
| You, on your machine | `gh auth login`: a browser window, or a pasted token | `git clone` for private checkouts, `uv`'s fetch when sources are private, `make images` |
| The devcontainer | the same login, forwarded as `GH_TOKEN`, or `gh auth login` inside it | the same |
| GitHub Actions | the **family GitHub App**, installed on the family repositories | a one-hour read-only token per CI job: `uv` fetches, the Docker build, the parity job's checkout of this repository |

Nothing else needs a GitHub credential. The running services never receive one.
Public hubs and clients fetch anonymously; no login is required to clone this
meta-repo and the public services in `repos.txt`. Private checkouts belong in
gitignored `.repos.local.txt` (see `.repos.local.txt.example`).

## Running Lucy on your machine

Clone this repository and bootstrap. Public services clone without a login; sign in
when you need private checkouts or write access:

```bash
gh auth login                 # only needed for private checkouts / write access
bash scripts/bootstrap.sh     # clones every repos.txt entry your account can read
make images && make up        # optional: the family on ports 8001–8008
```

You do **not** install or use the family GitHub App to run Lucy. The app exists only so
GitHub Actions can fetch private git sources without holding a person's session. On a
laptop, `gh` already is that session when a private repository is involved.

If you were invited to *this* family's private repositories, the same login is enough:
GitHub lets your account clone them, and CI on those repositories already uses the
owner's app.

If you copied the family under *your* GitHub account, local runs still use your `gh`
login. For CI, install the same public app on *your* repositories — see
[Your own copy](#your-own-copy).

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

## Connect CI: one command, one Install click

Actions cannot open a browser, and it must not hold your account's session. It uses the
public, read-only **lucy-assistant family CI** app plus the family token broker.

```bash
python scripts/connect_github.py        # or: make github-ci
```

1. Sign in with `gh` if you have not already (browser or a pasted token).
2. Your browser opens [the app's Install page](https://github.com/apps/lucy-assistant-family-ci).
   Click **Install**, choose **Only select repositories**, pick the repositories in this
   family, and confirm.
3. Return to the terminal and press Enter. The script starts one CI run.

That is all. The script creates no token and writes no Actions secret. `--dry-run`
explains the plan and touches nothing.

Afterwards the installation is listed under Settings → Applications → Installed GitHub
Apps. To let CI read a repository you add to the family later, add it there under
*Repository access*.

### What CI does with it

The caller grants `id-token: write`. GitHub signs a short-lived OIDC identity naming the
repository and the exact reusable workflow. The action sends that proof to the
Cloudflare Worker token broker. The broker verifies GitHub's signature, issuer,
audience, expiry, repository owner, and trusted workflow; then it mints a one-hour
read-only token for the repositories that owner chose when installing the app. It never
returns a token for another account's installation.

When no token can be minted, the action prints a warning saying why and the job goes on
without one: public sources still fetch anonymously, private ones fail at `uv sync` with
git's own message. The three reasons are a pull request from a fork (GitHub issues no
OIDC identity for those), an owner who has not installed the app yet, and a broker that
is unreachable.

The shared app's private key exists only as a Cloudflare Worker secret. A developer's
repository receives neither that key nor a long-lived token. The one-hour token reaches
git through process configuration and Docker through a BuildKit secret; it is masked in
Actions logs and never enters an image layer.

### Operating the broker (family owner only)

The broker is a Cloudflare Worker in [broker/](../broker/). Its page,
[broker/README.md](../broker/README.md), covers deploying it, giving it the app's
private key, rotating that key, and the two settings that pin which workflow it trusts.
Nobody else needs to touch it: installing the app is all a copy needs.

## Your own copy

Another developer clones this repository, adds their own API repositories to `repos.txt`,
and signs in with `gh auth login`. Local `make run` / `make up` use that login only.

For **their** GitHub Actions they install the **same** public app on **their**
repositories: `python scripts/connect_github.py` opens
<https://github.com/apps/lucy-assistant-family-ci>, they click Install, and the broker
mints tokens only for their installation. They never receive the app's private key.

`scripts/retarget.py THEIR_OWNER` rewrites clone URLs and tagged client source URLs.
The callers deliberately keep using the canonical public workflow at
`tochi-mba/LUCY-assistant@v1`, because that immutable workflow identity is what the
broker trusts. `--self-host-ci` is for operators running their own app and broker.

## Local image builds

```bash
make images    # here: GITHUB_TOKEN="$(gh auth token)" docker compose build
make docker    # in a service: the same, for that one image
```

Compose declares a build-time `github_token` secret sourced from `GITHUB_TOKEN`. It is a
build secret, not part of `.env.family`, and no running container sees it. Nothing
GitHub-related belongs in `.env.family`, a Docker `ARG`/`ENV`, or a committed file.

## Keep your copy private

Your service repositories and your copy of this repository may all be private. Their CI
callers should still reference the canonical public reusable workflow at
`tochi-mba/LUCY-assistant/.github/workflows/service.yml@v1`; public reusable workflows
can be called by private repositories. The canonical meta repository stays public
because it contains the audited workflow, broker client action, bootstrap, and
documentation—not service code or credentials.

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
