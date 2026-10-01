# Repositories

The `repos` capability lets Lucy read and change code, pull requests, issues and CI on the
account a person connected, and act when CI settles or a pull request merges. It is
optional, per profile, and every change is asked about unless the person already allowed
it. The model sees `repos.*` operations and nothing about the service behind them.

```
 person ──► Lucy hub (8000) ──► packs/repos.py ──► clients/repos.py ──► Github-api (8011) ──► api.github.com
                │  ▲                                                          │        │
                │  │ POST /v1/signals/{id} (HMAC, per-subscription secret)    │        └─ keyring: the person's
                │  └──────────────────────────────────────────────────────────┘           GitHub credential
                └─ keyring (8001): connects GitHub; records standing consent for watches
```

| Piece | Where | What it owns |
| --- | --- | --- |
| Capability | `src/lucy_api/packs/repos.py`, `packs/repos_calls.py` | operations, permissions, the probe, the setup card, watches |
| Contract client | `src/lucy_api/clients/repos.py` (`FakeReposClient` in `repos_fake.py`) | every route and field the hub reads |
| Service | [`tochi-mba/Github-api`](https://github.com/tochi-mba/Github-api) | GitHub's API, the person's credential, subscriptions and signals |
| Prompt page | `src/lucy_api/prompt/capabilities/repos.md` | what the model is taught |
| Decision | [ADR-0016](adr/0016-repos-capability-and-port-8011.md) | the name, the port, scoped grants, the provider word |

## Connecting: how much GitHub lets Lucy see

The person chooses this on GitHub, not in Lucy, and can change it there at any time.

- **The Lucy GitHub App** (recommended). `lucy connect repos`, or the setup card, opens
  keyring's OAuth flow for provider `github`. Installing the app is where the person picks
  **all repositories or only selected ones**, and the app's permission set bounds what any
  token can do. Tokens expire after eight hours and keyring refreshes them.
- **A fine-grained personal access token.** For a person who wants to choose repositories
  and permissions token by token: create it on GitHub, then store it in keyring with
  `PUT /v1/profiles/{profile}/connections/github/api-key`. The Lucy CLI never takes a secret.

Both are per keyring profile: connecting GitHub on `work` gives Lucy nothing on `personal`.
`repos.me` (and the live block) says which account, which kind of connection, and how many
repositories it can reach.

### Registering the Lucy GitHub App (operator, once)

On GitHub: *Settings → Developer settings → GitHub Apps → New GitHub App*.

| Field | Value |
| --- | --- |
| Callback URL | keyring's `KEYRING_OAUTH_REDIRECT_URI` |
| Request user authorization (OAuth) during installation | **on** |
| **Expire user authorization tokens** | **on — required** |
| Webhook | off (Github-api polls; see below) |
| Repository permissions | Contents, Pull requests, Issues, Actions, Checks, Workflows: read and write; Administration: read and write (create, delete, visibility); Metadata: read |
| Where can it be installed | any account |

Token expiry is not optional. Keyring assumes a one-hour life when a provider sends no
`expires_in`, and it refuses to renew a credential without a `refresh_token`; an app with
expiry off would break every connection an hour after consent. With it on, GitHub sends
`expires_in` (eight hours) and a refresh token, which keyring already reads.

Then add the app's client id and secret to keyring's `providers.json` (mode 0600, never in a
repository):

```json
{"github": {"authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "client_id": "Iv1....", "client_secret": "...", "scopes": []}}
```

## Choosing what runs without asking

Lucy's permission gate is the second layer, independent of GitHub's: GitHub decides what
the token *can* do; the person decides what Lucy may do *without asking*.

| Permission | Risk | Covers |
| --- | --- | --- |
| `repos.comment` | write, outward | `repos.comment`, `repos.review`, `repos.openIssue`, `repos.closeIssue` |
| `repos.change` | write, outward | `repos.commit`, `repos.branch`, `repos.openPull`, `repos.updatePull` |
| `repos.merge` | write, outward | `repos.merge` |
| `repos.ci` | execute | `repos.rerun`, `repos.dispatch`, `repos.cancelRun` |
| `repos.create` | write, outward | `repos.create` |
| `repos.destroy` | **destructive** | `repos.delete`, `repos.deleteBranch`, `repos.setVisibility` |
| `repos.watch` | write | `repos.watch` (and the standing consent it records) |

Every card offers the answers Lucy has everywhere: *yes once*, *this session*, *this
profile*, *always* (every profile), and *no*. Repository writes add one more:
**always, for this repository** — `{"lifetime": "profile", "only": ["owner/name"]}` on the
`input.approval` event, or `only` on `PUT /v1/permissions`. The same grant answered for a
second repository joins the first; an unlimited allow clears the limit. See
[ADR-0016](adr/0016-repos-capability-and-port-8011.md) for the rules.

- In `auto` mode an outward permission still asks until the person grants it
  (`confirm_outward_actions`).
- `repos.destroy` asks even in `auto` under the default `approval_policy`; only a stored
  allow skips it.
- `plan` mode runs reads only.

`GET /v1/permissions` lists all of them with the current grant (including `only`) for a
settings page; `DELETE /v1/permissions/{id}` returns one to "not yet asked".

## Operations

Reads: `repos.me`, `repos.find`, `repos.inspect`, `repos.pulls`, `repos.pull` (description,
reviews, open threads, CI, mergeability), `repos.issues`, `repos.checks` (one of `number` or
`ref`), `repos.log` (about 120 lines of one job, from `starting_at` or the end),
`repos.read`, `repos.tree`.

Writes: `repos.create`, `repos.setVisibility`, `repos.delete`, `repos.branch`,
`repos.deleteBranch`, `repos.commit` (up to 20 files), `repos.openPull`, `repos.updatePull`,
`repos.merge`, `repos.review`, `repos.comment`, `repos.openIssue`, `repos.closeIssue`,
`repos.rerun`, `repos.cancelRun`, `repos.dispatch`.

Every write takes `repo` (and `number` where it applies) as plain text, never a
`$reference`: the gate matches "always, for this repository" against the value it can see
before the plan runs.

New repositories go to the person's default owner and visibility from settings namespace
`github` (`default_owner`, `default_visibility`) when the model names neither.

## Watching: CI can wake a session

`repos.watch` is the first capability on [jobs and signals](jobs.md). It asks Github-api for
a subscription (`checks_settled`, `pull_merged`, `review_submitted`, `run_completed`), hands it
a signal URL and a fresh secret, and returns a `subscription` handle at once. When it
happens, Github-api signs and posts the signal; the hub ends the work and, with `wake`, opens
a turn on the idle session.

With `wake`, the person's yes also records **standing consent**: a keyring grant for the
hub, for the life of the watch plus fifteen minutes. The woken turn exchanges it for a token
and is prepared exactly like a turn the person sent — same settings, same grants, same gate —
so "merge #42 when CI is green" ends with the merge, or with an approval card if the person
never allowed merges. The grant is in the person's own list, revocable there; cancelling the
watch withdraws it. A restart loses nothing: the row is durable and the sweep asks
Github-api about any signal it might have missed.

## Running it

| Variable | Default | |
| --- | --- | --- |
| `LUCY_REPOS_API_BASE_URL` | `http://127.0.0.1:8011` | where the contract is served |
| `LUCY_REPOS_API_AUDIENCE` | `github-api` | the implementation's audience (keyring mints for it) |
| `LUCY_SIGNAL_BASE_URL` | `http://127.0.0.1:8000/v1/signals` | what Github-api posts signals to |

`scripts/genenv.py` registers `github-api` as a keyring consumer (`GHAPI_KEYRING_SERVICE_TOKEN`),
an exchange audience, and a settings reader of namespace `github`.

**Not yet in compose or `repos.txt`.** Github-api joins `repos.txt`, `repos.lock`,
`docker-compose.yml` (service `github`, port 8011) and the workspace file in the same change
that publishes the repository, so a fresh `make bootstrap` never clones a repository that is
not there. Until then the capability probes as `unavailable` and binds nothing.

## Testing

`tests/hub/test_clients_repos.py` (every route, header and error), `test_repos_pack.py`
(availability, every operation against `FakeReposClient`, permissions, watching through a
real `Subscriptions`), `test_repos_fake.py`, `test_permissions_only.py` (scoped grants), and
`test_standing.py` (acting under a grant).
