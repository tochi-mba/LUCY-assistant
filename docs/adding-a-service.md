# Adding a service to the family

Copy [examples/hello-api](../examples/hello-api) when you need a new HTTP service.
That tree is a complete JWKS-only FastAPI service: family Makefile verbs, the OIDC
CI caller, `/healthy` and `/ready`, config that refuses unknown `HELLO_*` variables,
and one authenticated route. It is not itself a family member and is not listed in
`repos.txt`.

## 1. Copy and rename

```bash
cp -R examples/hello-api ../Your-api
cd ../Your-api
```

Rename the package (`hello_api` → `your_api`), the env prefix (`HELLO_` → `YOUR_API_`),
the audience (`hello` → your service audience), the port, and the Docker/Make image
names. Keep `/healthy` and `/ready` spelled exactly that way.

## 2. Choose the auth pattern

| Pattern | When | What to add |
| --- | --- | --- |
| **JWKS-only** (hello-api, User-api, Persona-api, Settings-api) | You only need to know *who* is calling | `keyring_client` verifier; no `KEYRING_SERVICE_TOKENS` entry |
| **Credential consumer** (Spotify-api, Web-search-api, Environments-api, …) | You fetch third-party secrets from keyring | Service token + `CredentialClient`; add the service to `scripts/genenv.py` `KEYRING_CONSUMERS` |
| **Settings reader** | Per-person knobs | `settings-client` and a grant in Settings-api |

Shared clients come from the **public** hubs:

```toml
[tool.uv.sources]
keyring-client = { git = "https://github.com/tochi-mba/Keyring-api", subdirectory = "clients/python", tag = "keyring-client-v0.1.0" }
settings-client = { git = "https://github.com/tochi-mba/Settings-api", subdirectory = "clients/python", tag = "settings-client-v0.2.0" }
```

Clones can fetch those tags anonymously from the public Keyring-api and Settings-api
hubs.

## 3. Join the family desk

1. Create the GitHub repository (public or private) and push the copy.
2. Append one line to this meta-repo's `repos.txt` (public) or `.repos.local.txt`
   (gitignored; copy from `.repos.local.txt.example`): `Your-api https://github.com/<owner>/Your-api.git`.
3. Re-run `bash scripts/bootstrap.sh` (or the PowerShell bootstrap) so the checkout appears beside this file.
4. Add the repository to the [lucy-assistant family CI](https://github.com/apps/lucy-assistant-family-ci) installation (Settings → Applications → Installed GitHub Apps → Repository access).
5. From this meta-repo: `python scripts/parity.py --repo Your-api`.

Optional: wire a **public** service into `docker-compose.yml`, `scripts/genenv.py`,
and `LUCY-assistant.code-workspace` when it should run with the rest of the family.
A private checkout is listed only in gitignored `.repos.local.txt` and, for the
editor, in a gitignored `*.local.code-workspace` — never in the committed workspace.
Family ports are 8000–8009, and every number in that block is assigned or reserved
([ADR-0004](adr/0004-port-assignments.md), [ADR-0010](adr/0010-ports-8000-and-8009.md)).
A new service that runs in compose needs a number outside it -- 8010 is the optional Laya
service's and 8011 is Github-api's ([ADR-0016](adr/0016-repos-capability-and-port-8011.md))
-- chosen in an ADR that amends ADR-0010. A service with work that outlives a request
speaks the [jobs and signals](jobs.md) contract and depends on `lucy_signals`.

## 4. CI caller

Every service uses the same thin caller. No Actions secrets:

```yaml
name: CI
on:
  push:
    branches: ["**"]
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
  id-token: write
jobs:
  service:
    uses: tochi-mba/LUCY-assistant/.github/workflows/service.yml@v1
```

## 5. Give Lucy a capability for it

A service the family runs is not yet something Lucy can use. The model sees capabilities,
never services, so the hub needs a pack that speaks for it. Use `packs/research.py` and
`clients/search.py` as the pattern; every path below is under `src/lucy_api/`.

1. **A client**, `clients/<sibling>.py`: a `Protocol` at the seam, the HTTP implementation
   on `clients.transport.Sibling`, sent through the turn's `Http`, which mints a token for
   the service's audience and never forwards the caller's; and a hand-written `Fake`
   beside it for the tests.
2. **A pack**, `packs/<id>.py`, satisfying `CapabilityPack` in `packs/base.py`: `id`,
   `title` and `summary`; `operations()`, each one `define_operation` with a name of the
   form `<id>.<verb>`, a description, an input schema, an output and its `effects`;
   `permissions()`, one `Permission` covering its writes; `probe()`, which says whether it
   is usable for this person right now; `setup()`; and `result_trust()`. An operation that
   returns a list declares a collection in `packs/collections.py` and adds it to `ALL`.
3. **A page** for the model, `prompt/capabilities/<id>.md`, returned by the pack's `docs`
   through `capability_doc("<id>")`. `tests/hub/test_prompt_docs.py` requires exactly one
   page per pack, a single heading, a length ceiling, and no port, HTTP verb or service
   name in it.
4. **Wire it in**: a `<name>_base_url` field in `core/config.py` (then `.env.example`,
   [operations.md](operations.md) and the `lucy` service in `docker-compose.yml`), and the
   pack in `installed_packs()` in `packs/service.py`, which `core/container.py` calls with
   the settings.
5. **Let keyring mint for it**: the service's audience in `LUCY_EXCHANGE_AUDIENCES` in
   `scripts/genenv.py`, and a settings grant in `SETTINGS_GRANTS` if the hub reads its
   namespace.
6. **A setup card**, in `onboarding/catalogue.py`, so `GET /v1/setup` and `lucy connect`
   can say what the capability needs.

A private service does none of this in the public tree; see
[private-repos.md](private-repos.md).

## Checklist

- [ ] `make check` is green on 3.12 (and `make matrix` before a push).
- [ ] `python scripts/parity.py --repo Your-api` is green.
- [ ] Audience is documented; keyring mints for it with `POST /v1/auth/service-token`.
- [ ] No GitHub token in `.env`, Docker `ARG`/`ENV`, or committed files.
- [ ] Dockerfile keeps `# syntax=docker/dockerfile:1` and a `github_token` secret mount on every `uv sync` RUN.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the family standard and
[private-repos.md](private-repos.md) for sign-in and the shared CI app.
