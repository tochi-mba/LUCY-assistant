# Private repositories and GitHub sign-in

Every repository in this family can be private. Three things need to read them, and each
signs in differently:

| Who | Signs in with | What it is used for |
| --- | --- | --- |
| You, on your machine | `gh auth login`: a browser window, or a pasted token | `git clone` in bootstrap, `uv`'s fetch of the client packages, `make images` |
| The devcontainer | the same login, forwarded as `GH_TOKEN`, or `gh auth login` inside it | the same |
| GitHub Actions | the `FAMILY_GITHUB_TOKEN` secret: a fine-grained token, read-only, family only | `uv` fetches in CI, the Docker build, the parity job's checkout of this repository |

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

## Give CI a read-only token

Actions cannot open a browser, and it must not hold your account's session. It gets its
own token, which can read the family and nothing else.

1. Open <https://github.com/settings/personal-access-tokens/new>.
2. **Resource owner:** the account or organisation that owns the family.
3. **Repository access:** *Only select repositories*, then pick all nine: this repository
   and the eight listed in `repos.txt`.
4. **Permissions → Repository permissions → Contents:** *Read-only*. Metadata is added
   automatically. Nothing else.
5. **Expiration:** up to a year. Note the date; rotating is one command, below.
6. Generate it and copy the value. It starts with `github_pat_`.

Then, from this directory, signed in with your own account:

```bash
python scripts/share_github.py --dry-run   # asks for the token, checks it reads all nine, sets nothing
python scripts/share_github.py             # sets FAMILY_GITHUB_TOKEN on all nine repositories
```

`make github-ci` is the same command. The script reads the token from a hidden prompt,
from `$FAMILY_GITHUB_TOKEN`, or from `--stdin` (a password manager), never from an
argument, and it never prints it. It refuses a classic token or a `gh` session
(`ghp_`/`gho_`): those can write to every repository on the account, and CI only needs to
read nine. It stops, naming the repository, when the token cannot read one of them.

To rotate, generate a new token and run the script again. `gh secret list --repo
OWNER/REPO` shows the names of the secrets that are set, never their values.

### What CI does with it

The reusable workflow declares the secret and each caller passes it with
`secrets: inherit`. Every job that runs `uv sync` first tells git to use it for
`github.com`, through git's configuration rather than the log. The Docker job passes it as
a BuildKit secret, mounted only while `uv sync` runs, so it never lands in an image layer.
The parity job checks out this repository with it and does not persist it.

When the secret is empty, as on a fork without it, CI still runs: git fetches anonymously,
which works while the sources are public and fails with a clear message when they are not.

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

1. Install the token as above.
2. Make the **eight services** private. In the browser: repository → Settings → General →
   Danger zone → Change visibility. Or:

   ```bash
   gh repo edit OWNER/Keyring-api --visibility private --accept-visibility-change-consequences
   ```

   Re-run one consumer's CI; it now fetches private client tags with the token.
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

`make test` covers the compose file, the workflow, the Makefiles and both bootstraps with
fake tools, without touching your GitHub session. Two opt-in checks use Docker:

```bash
LUCY_TEST_DOCKER=1 uv run --with pytest pytest tests/test_build_secrets.py -q          # a sentinel secret never reaches a layer
LUCY_TEST_FAMILY_IMAGES=1 uv run --with pytest pytest tests/test_image_runtime.py -q  # after make images
```

The string `x-access-token` appears in Dockerfile metadata by design; a leak would be the
token's value inside a layer, which the first test looks for byte by byte.
