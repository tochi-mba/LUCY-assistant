# The family token broker

A Cloudflare Worker that turns a GitHub Actions job's identity into a one-hour, read-only
token from the **lucy-assistant family CI** GitHub App. It exists because the app's
private key must live in exactly one place, and that place cannot be a repository secret
in nine (or ninety) repositories.

Only the family owner runs a broker. Everyone else installs the app on their repositories
(`python scripts/connect_github.py` from the meta-repo) and their CI uses this one.

## What it does

```
job (service.yml@v1) ──OIDC identity──▶ broker ──app JWT──▶ GitHub ──installation token──▶ job
```

`POST /v1/token` with `Authorization: Bearer <GitHub OIDC token>`:

1. Verifies the identity against GitHub's published keys: RS256 signature, issuer,
   audience `lucy-assistant-family-ci`, expiry, and that `job_workflow_ref` is exactly
   `TRUSTED_WORKFLOW`. Any other workflow, on any repository, is refused with 403.
2. Finds the app's installation on the calling repository's owner (403 if none:
   _install the family app on this repository owner first_).
3. Mints an installation token with `contents: read` and `metadata: read` for the
   repositories that owner selected when installing, and returns it with its expiry.

`GET /healthy` answers `{"status":"ok"}`. Everything else is 404. The body of a token
request is ignored; the request's identity is the only input.

## Configuration

| Setting            | Where                   | Value                                                                 |
| ------------------ | ----------------------- | --------------------------------------------------------------------- |
| `APP_CLIENT_ID`    | `wrangler.jsonc` `vars` | the app's client id, also in `family-app.json`                        |
| `OIDC_AUDIENCE`    | `wrangler.jsonc` `vars` | `lucy-assistant-family-ci`; the action requests this audience         |
| `TRUSTED_WORKFLOW` | `wrangler.jsonc` `vars` | `tochi-mba/LUCY-assistant/.github/workflows/service.yml@refs/tags/v1` |
| `APP_PRIVATE_KEY`  | Worker secret           | the app's private key, PEM (PKCS#8 or PKCS#1)                         |

Because `TRUSTED_WORKFLOW` names the `v1` tag, moving that tag is what changes the code
that may hold a token. Only people who can push to the meta-repo can move it.

## Deploy

```bash
cd broker
npm ci
npx wrangler login                      # once per machine; opens the browser
npx wrangler secret put APP_PRIVATE_KEY # paste the PEM, end with Ctrl-D (Ctrl-Z Enter on Windows)
npx wrangler deploy
curl -s https://lucy-family-ci-token-broker.lucy-assistant-family.workers.dev/healthy
```

The private key comes from the app's settings page: GitHub → Settings → Developer
settings → GitHub Apps → _lucy-assistant family CI_ → _Private keys_ → _Generate a
private key_. GitHub downloads a `.pem` once; paste it into `wrangler secret put` and
delete the file. Nothing in this repository ever holds it.

## Rotate the key

Generate a new private key on the app's settings page, `npx wrangler secret put
APP_PRIVATE_KEY` with the new PEM, `npx wrangler deploy`, then delete the old key on the
same page. Jobs that were mid-flight keep their one-hour tokens; new jobs use the new key
immediately.

## Change what is trusted

To trust a different ref, edit `TRUSTED_WORKFLOW` in `wrangler.jsonc` and deploy. To run
a separate family with its own app and broker, create the app (Contents: read-only,
Metadata: read, no webhook), set `APP_CLIENT_ID`, point `TRUSTED_WORKFLOW` at your
workflow, deploy, and retarget the family with `scripts/retarget.py OWNER --self-host-ci`
so callers use your workflow and the action's `broker-url` names your Worker.

## Check it

```bash
npm run check   # prettier, then the tests in test/ (fake GitHub, generated keys)
```

The meta-repo's CI runs the same command on every push.
