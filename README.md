# LUCY-assistant

Lucy is the assistant hub in `src/lucy_api/`. This repository also holds the **family
desk**: tools and documentation for eight public sibling services, each in its own git
repository. `scripts/bootstrap.sh` clones those checkouts beside this file. Operator-local
services attach through the documented extension points and a private Compose overlay.

The hub currently provides health, readiness, identity, session management, a durable
single-agent conversation runner, resumable event streaming, capability packs, child
helpers, MCP in both directions, and a command-line setup flow. Configure an OpenAI or
Anthropic API key to run a basic conversation. Memory, persona and pinned account facts
reach the model as separate sections; their retrieval scores are never merged.
Installing the client does not yet provide a chat command.

`make check` is the same four gates everywhere it exists: lint, types, imports, tests at
100% branch coverage. `python scripts/parity.py` is how we notice when a checkout has
drifted.

**The published family repositories are public by default.** Bootstrap clones every
service listed in `repos.txt` (plus optional gitignored `.repos.local.txt`) that your
account can read; public ones need no login. Sign in with `gh` when you need private
checkouts, forks, or write access. CI uses the shared family GitHub App over OIDC, not a
personal token.

## Workflows Lucy generates

Lucy is designed to turn a request into a plan of named operations. A later step can use
an earlier result through a reference such as `$hits`, rather than asking the model to
copy the result into another tool call. Lucy can inspect a result and generate another
plan when the next decision depends on what it found.

**Implementation status:** the examples below describe the intended conversation
workflows. The durable `/inputs` path can run plain assistant turns when a model provider is
configured; the concrete capability packs used by these examples are still being connected.
Argument names below illustrate the proposed pack contracts. The per-step `note` field is planned upstream work (W12), not a feature
of the currently pinned weftai release. See [implementation status](docs/implementation-status.md).

Each example separates the person's request, the plan, and what happens around execution.
A plan describes work; it does not grant permission to perform it. References remain
scoped to the person's session, and the model never handles the credentials used to run
an operation.

### 1. Research a question and save the useful evidence

> “Find recent evidence about heat pumps in older UK houses. Save a short briefing with
> sources, and tell me where the evidence disagrees.”

An initial plan gathers candidates:

```json
{
  "steps": [
    {
      "id": "hits",
      "op": "research.search",
      "input": {"query": "heat pump retrofit older UK houses field study"},
      "note": "Find field studies of heat pumps in older UK homes"
    }
  ]
}
```

The model receives a concise projection: titles, URLs, short summaries and a handle to the
stored results. It compares the sources before deciding which to open. It should not
assume the first search hit supports the requested conclusion.

The next plan opens selected sources and saves a briefing. In this illustrative shape,
`from` accepts a stored reference and `workspace.write` knows how to save that result:

```json
{
  "steps": [
    {
      "id": "evidence",
      "op": "research.open",
      "input": {"from": "$hits[0]"},
      "note": "Read the strongest candidate and preserve its source"
    },
    {
      "id": "brief",
      "op": "research.summarize",
      "input": {"from": "$evidence"},
      "note": "Summarise the findings and their limitations"
    },
    {
      "id": "saved",
      "op": "workspace.write",
      "input": {"path": "research/heat-pumps.md", "from": "$brief"},
      "note": "Save the evidence briefing in this session's workspace"
    }
  ]
}
```

`evidence → brief → saved` is a dependency chain. Saving waits for the summary, and the
summary waits for the source. The complete source text can stay in the result store;
the model sees the portion needed to assess it. Comparing several sources would open
several selected results and retain provenance for each.

A workspace write pauses if permission is needed. If opening a source fails, dependent
steps are skipped and Lucy explains what is missing instead of saving an invented
briefing. Web-page text remains untrusted evidence, even when it contains instructions.

### 2. Play yesterday's music, then review repository changes

> “Play what I had on yesterday, then summarise what changed in the repo.”

```text
recent = music.recent(yesterday)
devices = music.devices()
→ inspect listening history and available devices
→ ask which session or device if there is more than one plausible choice
playback = music.play(selected track or context, selected device)
changes = workspace.run(read-only git status and git log commands)
→ explain the repository changes, with file references
```

Listening history and device discovery are independent reads and can happen together.
Playback needs both results and the applicable permission. “Yesterday” is resolved in
the person's timezone, rather than the server's timezone.

The repository summary is a different capability. If playback cannot start because no
device is active, Lucy can report that state and still review the repository when that
fits the request. It must not report music as playing merely because a request was sent.

If music is unconnected, playback operations are absent from Lucy's model-facing tools.
The always-available capability discovery explains how to connect it; after connection,
the next turn probes again and can offer music operations. Passwords and tokens never
belong in the conversation.

### 3. Fix an unfamiliar file without overwriting somebody else's work

> “The settings page crashes when a profile has no display name. Find the cause and fix it.”

```text
matches = workspace.grep("display_name")
window = workspace.read(the relevant file, a bounded line window)
→ inspect the code and choose a small change
change = workspace.edit(path, unique old text, replacement, fingerprint)
verification = workspace.run(the relevant test)
→ report the change, the test result, and any remaining uncertainty
```

Search narrows the files before a read spends context on them. The read returns numbered
lines, total size and a fingerprint. The edit uses content as its anchor; line numbers
help the model navigate but are not stable editing identifiers.

If the file changes after the read, the edit fails with a stale-fingerprint explanation.
Lucy reads the changed region and prepares a new edit. If the old text appears twice,
the error names both locations so the next attempt can use a unique anchor. It must not
replace an arbitrary occurrence.

The write goes through the permission gate before changing the file. Verification follows
the edit because it depends on the new contents. A failing test becomes evidence for the
next turn of the loop, not a reason to claim the task succeeded.

### 4. Apply one rule across many files

> “Update every Python package in this workspace to require Python 3.12, and show me the diff.”

```text
inventory = workspace.grep("requires-python", pyproject files)
→ read the relevant declarations and decide the transformation
script = workspace.write("scripts/update_python_floor.py", the transformation)
preview = workspace.run("python scripts/update_python_floor.py --check")
→ inspect the proposed paths and changes
→ approve the bulk write if required
→ take a workspace checkpoint
applied = workspace.run("python scripts/update_python_floor.py --apply")
verification = workspace.run(package validation and git diff)
```

The generated script is a saved artifact with a sentence explaining its purpose. Its
check mode reports what it would change; its apply mode performs the reviewed rule.
This avoids making the model copy every file's contents through its context.

Writing the script is itself a workspace write and follows the current permission mode.
Executing its apply mode is a separate action whose approval names the script and the
expected paths. A checkpoint precedes execution so an incorrect transformation can be
reviewed and reverted. A dry run is useful evidence, not proof that arbitrary code is safe.

The final answer links the script and summarises the actual diff and validation results.
Files that could not be parsed are reported explicitly rather than silently skipped.

### 5. Remember a correction and use it in another conversation

> Monday: “I moved to Bristol in March. Remember that.”
>
> Friday: “Find something local to do this weekend.”

```text
Monday:
  existing = notes.search("home city")
  → reconcile the new statement with the existing fact
  updated = notes.correct(existing memory, "Bristol", valid from March)
  → show the person what was remembered

Friday:
  remembered = notes.search("home city")
  suggestions = research.search(events near the current remembered city)
  → answer with dates and sources
```

If no previous memory exists, Lucy uses `notes.remember` instead of inventing an id to
correct. A correction preserves the previous record's provenance and validity period;
it does not leave two equally current home cities in retrieval.

The remembered fact reaches the model as a reported claim with its origin and date.
If the date is ambiguous, Lucy asks rather than inventing a year. A city found on a web
page is not automatically promoted into a fact about the person.

Incognito sessions do not retrieve or write memory. Forgetting and erasure remain visible
operations, with their documented retention behavior; a correction is not the same as
permanently erasing the old record.

### 6. Decline an action and teach Lucy the alternative

> “Clean up the old drafts.”
>
> At the approval prompt: “No — move them to trash instead. Never delete drafts outright.”

```text
files = workspace.list("drafts/")
→ identify the proposed files and request approval before deleting anything
person denies with an instruction
→ record the refusal and return it to the model as a tool result
→ retain the scoped behavior lesson and permission decision
replacement = workspace.move(the reviewed files, "trash/")
→ evaluate permission for this different action before executing it
```

The prompt says what Lucy intends to do in plain language and makes the affected paths
inspectable. Denying the action leaves those files unchanged. The refusal is useful input
for the next plan, so Lucy can choose the alternative rather than end with an exception.

The instruction's lifetime and profile/account scope are explicit. Permission to move is
checked separately: denial of deletion does not automatically authorize every possible
replacement. Neither a tool result nor a child agent's message can stand in for the
person's approval.

### 7. Ask independent reviewers, then make one coordinated change

> “Compare two approaches to caching these responses, then implement the safer one.”

```text
reviewer_a = agents.spawn(read-only review of approach A)
reviewer_b = agents.spawn(read-only review of approach B)
→ parent inspects the current implementation while the reviewers work
agents.send(reviewer_b, "Account isolation is a hard requirement")
→ receive bounded reports with file and result references
→ parent decides, edits, and runs verification
```

Each child gets an objective, boundaries, output format, effort budget and the decisions
already made. The branches should be independent enough that their intermediate findings
do not need to travel through the parent's context.

The parent is the workspace writer. Children return short reports and references, not
competing patches or their entire transcripts. Steering messages enter at tool boundaries;
a child finishing pushes its result so the parent need not repeatedly poll.

If one reviewer fails, its result says so. The parent can narrow the decision, replace the
review, or explain the missing evidence. A child report is untrusted input and cannot
broaden permissions, even if it recommends running a command.

### 8. Resume a long investigation after closing the laptop

> “Investigate the intermittent failure and leave me a report. I'll check back later.”

```text
input accepted → durable turn queued → model/tool rounds recorded
→ client disconnects; the turn continues
→ completed tool outputs remain in the result store
→ client reconnects using starting_after
→ snapshot establishes current state; subsequent events update it
```

Closing the stream is not cancellation. An explicit cancel targets a particular turn so
an old stop request cannot accidentally cancel newer work.

A server restart is different from a client disconnect. Durable execution must reconcile
unfinished steps and reuse recorded completed results. An uncertain non-idempotent action
cannot simply be run again: Lucy needs an idempotency key or a way to reconcile its outcome.
Before resuming workspace work, it reads the progress journal, task list and checkpoint
history to recover what changed.

As context fills, old tool output can leave the assembled prompt while remaining
addressable by reference. Compaction creates a projection over the preserved transcript;
it does not rewrite the original conversation. The final report includes what was
verified, what remains unresolved, and the artifacts produced.

## Services

| Service | Purpose | Port | Env prefix | Health |
| --- | --- | ---: | --- | --- |
| **Lucy** (this repository) | The assistant hub: health, readiness and caller identity; conversation support is in development. | 8000 | `LUCY_` | `/healthy`, `/ready` |
| [Keyring-api](https://github.com/tochi-mba/Keyring-api) | Accounts, profiles, and the credential vault. Issues the tokens everyone else verifies. | 8001 | `KEYRING_` | `/healthy`, `/ready` |
| [User-api](https://github.com/tochi-mba/User-api) | Structured facts about the **person** the assistant is talking to. | 8002 | `USER_API_` | `/healthy`, `/ready` |
| [Settings-api](https://github.com/tochi-mba/Settings-api) | Per-person knobs that used to live as process-wide env vars. | 8003 | `SETTINGS_API_` | `/healthy`, `/ready` |
| [Persona-api](https://github.com/tochi-mba/Persona-api) | The assistant's model of **itself** (fields and notes, one persona per profile). | 8004 | `PERSONA_` | `/healthy`, `/ready` |
| [Web-search-api](https://github.com/tochi-mba/Web-search-api) | Search, scrape, and summarise with a provider resolved per caller. | 8006 | `WSA_` | `/healthy` (alias `/health`), `/ready` |
| [Spotify-api](https://github.com/tochi-mba/Spotify-api) | Batch track lookup and confirmed playback. Holds no Spotify credential. | 8007 | `SPOTIFY_API_` | `/healthy`, `/ready` |
| [Environments-api](https://github.com/tochi-mba/Environments-api) | Sandboxed shells. Remote code execution as a product; needs Linux. | 8008 | `ENVAPI_` | `/healthy` (alias `/health`), `/ready` (alias `/health/ready`) |
| [Memory-api](https://github.com/tochi-mba/Memory-api) | What the assistant has learned about the person: provenance, history, and a topic index. | 8009 | `MEMORY_` | `/healthy`, `/ready` |

Every public service listens on its assigned port, so the hub and its siblings run on one
host without a collision. Compose maps each host port to the same number inside the
container. Private overlays choose their own non-conflicting ports.

**`/healthy` is liveness and `/ready` is readiness**, everywhere. Liveness does no I/O and
never fails, because an orchestrator restarts a container whose liveness check fails and
restarting a process does not fix the service it depends on. Readiness reports each
dependency and answers 503 when one is unusable. Point container healthchecks at the first
and load balancers at the second.

## Token flow

A person logs into keyring and holds an **opaque session**. Anything that acts for them
asks keyring to mint a short-lived **Bearer JWT** for a named audience, then presents
that token to the service. The service verifies it locally against keyring's JWKS. If it
needs a third-party credential, it calls keyring's `/v1/internal` with its own service
token *and* the user's token. If it needs a per-person knob, it calls settings-api the
same way.

```mermaid
sequenceDiagram
  participant Person
  participant Keyring
  participant Service
  participant Settings
  Person->>Keyring: POST /v1/auth/login
  Keyring-->>Person: opaque session
  Person->>Keyring: POST /v1/auth/service-token (audience = the service)
  Keyring-->>Person: Authorization Bearer JWT
  Person->>Service: Authorization Bearer JWT
  Service->>Keyring: GET /.well-known/jwks.json (cached)
  Service->>Keyring: GET /v1/internal/credentials (service token + user token)
  Service->>Settings: GET /v1/internal/settings/{ns} (service token + user token)
```

Authorization is **Bearer**, everywhere. No `X-API-Key` as the identity of a person.
(A couple of services still accept an optional network-level API key on top; that is
not who the request is *for*.)

## Who calls whom

```mermaid
flowchart LR
  Keyring -->|"JWKS"| User
  Keyring -->|"JWKS"| Settings
  Keyring -->|"JWKS"| Persona
  Keyring -->|"JWKS + /v1/internal"| Search
  Keyring -->|"JWKS + /v1/internal"| Spotify
  Keyring -->|"JWKS + /v1/internal"| Environments
  Person([assistant / MCP]) --> Keyring
  Person --> User
  Person --> Settings
  Person --> Persona
  Person --> Search
  Person --> Spotify
  Person --> Environments
```

User-api, Persona-api, and Settings-api never call keyring at request time except to
fetch public keys. They have **no** entry in `KEYRING_SERVICE_TOKENS`. Spotify-api,
Web-search-api, and Environments-api do: they resolve credentials per request.
Settings grants exist for credential consumers so wiring a settings client is a
deployment choice, not a settings-api release.

Nothing in this family calls User, Persona, Search, Spotify, or Environments except
the assistant sitting in front.

## Your first hour

For the command-line client, install [uv](https://docs.astral.sh/uv/getting-started/installation/),
clone this repository, then run either installer. Its path can be absolute; your working
directory does not matter.

```bash
bash scripts/setup.sh
# PowerShell: pwsh scripts/setup.ps1
```

The installer puts `lucy` on your user PATH through `uv tool install --editable`, then
opens `lucy setup`. Choose **hub** for a local development hub, **family** for the local
Compose stack, or **remote** for an existing hub URL. Setup saves client configuration
and explains the remaining steps; it does not start services. Use `--dry-run` to preview
the installer or `--skip-setup` to install only. Keep this checkout in place because the
editable command imports its code from here.

```bash
lucy config                     # effective configuration, with the token redacted
lucy doctor                     # independent checks and fixes
lucy connect                    # discover capability setup support from your hub
```

Signing into Lucy currently means providing a Keyring-issued JWT with audience
`lucy-api`, using the hidden setup prompt, `LUCY_TOKEN`, or `--token-stdin`. Browser and
device sign-in are not implemented yet. You can skip the token and finish later.
[The CLI guide](docs/cli.md) covers remote setup, automation and diagnostics.

For family service development:

1. Open this folder in a [devcontainer](.devcontainer/devcontainer.json) or on
   Linux/macOS/WSL2. Native Windows without WSL2 can run most services; Environments-api
   cannot.
2. `bash scripts/bootstrap.sh` or `pwsh scripts/bootstrap.ps1`. That checks for `uv`,
   Python 3.12/3.13, `make`, `git`, `gh`, `jq`, `sqlite3`, reports Docker without
   installing it, asks you to sign in to GitHub (browser or a pasted token) if you are
   not already, clones any missing checkout your account can read from `repos.txt`, and runs
   `make install` unless you pass `--no-install`. The family GitHub App is only for CI;
   local development uses your own GitHub sign-in.
3. In Keyring-api: copy `.env.example`, set `KEYRING_MASTER_KEY`, `make run`.
4. Mint a service token for the service you are working on
   (`POST /v1/auth/service-token` with that service's audience) and put the Bearer on
   the request.
5. `make check` in that repository. `python scripts/parity.py` from here for the family
   scoreboard — it scores the hub too.

To work on **Lucy herself**, you are already in the right directory:

```bash
make install
make run                          # http://127.0.0.1:8000/docs
curl localhost:8000/healthy       # liveness: no I/O, never fails
curl localhost:8000/ready         # readiness: 503 until keyring is up, and it says so
make check                        # lint, types, imports, tests at 100% branch coverage
```

To run the whole family with Docker Engine running:

```bash
python scripts/genenv.py          # writes .env.family; never prints the values
make images && make up            # host ports 8000–8009; up reuses the build cache
```

Re-running `genenv.py` refuses to overwrite `.env.family` unless you pass `--force`.

## The `lucy` command

`lucy` manages client setup and checks a running hub from any directory. The installer
above puts it in its own isolated environment; inside this repository, `uv run lucy`
also works without a global installation.

```bash
lucy setup                          # choose hub, family, or remote; save client settings
lucy status                         # alive, ready, which dependency is down, and who you are
lucy status --json | jq .ready      # the same, as a contract a script can depend on
lucy serve                          # run the hub here, in the foreground
```

It is a **client**: the same command works against a hub on this laptop, in compose, or on
another machine. `--url` overrides `LUCY_URL`, which overrides the saved URL. Your token
comes from `LUCY_TOKEN` or the saved configuration and is never a flag value. Exit
codes are `0` worked, `1` the answer was no, `2` bad command, `3` hub unreachable.
[docs/cli.md](docs/cli.md) has the rest.

## Signing in to GitHub

```bash
gh auth status               # which account is active
gh auth login                # a browser window, or paste a token: gh asks which
bash scripts/bootstrap.sh    # signs you in if you skipped that, then clones
make images                  # local builds use the same sign-in
```

Bootstrap runs `gh auth login` when you are not signed in and a terminal is available,
then `gh auth setup-git`, so `git clone` and `uv`'s fetches of the client packages use
that account. A repository your account cannot see is reported and skipped; existing
checkouts are left alone. Without a terminal, set `GH_TOKEN`; the devcontainer forwards
yours and re-runs bootstrap when a terminal attaches.

CI cannot open a browser, so it uses the public **lucy-assistant family CI** app. That
app is **not** how a clone on your laptop authenticates: a laptop uses the `gh` login
above. `python scripts/connect_github.py` opens
<https://github.com/apps/lucy-assistant-family-ci>; you click **Install** on the family
repositories. Each CI job proves its identity with GitHub OIDC; the family broker then
mints a one-hour read-only token for that installation. Another developer who cloned
this repo and added their own API repositories runs the same command and installs
**the same app** on *their* repositories. They receive neither our app private key nor
a long-lived token. Never put a GitHub credential in `.env.family`, a Docker build
argument, an Actions secret, or a committed file.
[docs/private-repos.md](docs/private-repos.md) is the full walkthrough. On native Windows,
run Make recipes in Git Bash; bootstrap also has a PowerShell version.

## Adding a repository to the family

Start from [examples/hello-api](examples/hello-api) and follow
[docs/adding-a-service.md](docs/adding-a-service.md). Append one line to `repos.txt`
(public) or `.repos.local.txt` (gitignored, for private checkouts only):
`<folder> <https clone URL>`. Re-run bootstrap to clone it. For the family checks, give
the service this caller in `.github/workflows/ci.yml`:

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

Add the new repository to the family app's installation (GitHub → Settings →
Applications → Installed GitHub Apps → the family app → Repository access) so CI can read
it, then `python scripts/parity.py --repo <folder>`. Bootstrap discovery needs only the
manifest line; adding a running service to Compose or a folder to the IDE workspace is a
separate choice.

## Keeping your copy private / Forking

Copy this repository and the siblings listed in `repos.txt` under one owner, keeping
their names and client tags.
GitHub forks inherit their network's visibility: a fork of a public repository cannot
be made private by itself. Use private standalone copies when the upstream is public,
or private forks when GitHub permits them. See [GitHub's fork rules](https://docs.github.com/en/pull-requests/reference/forks).

From your copy of this meta-repo, with the service checkouts present:

```bash
python scripts/retarget.py YOUR_OWNER --dry-run
python scripts/retarget.py YOUR_OWNER
# Run each printed `uv lock --directory ...` command, review and commit the lockfiles.
```

The script changes manifest clone URLs and tagged client source URLs. It preserves line
endings and never edits `uv.lock`, git remotes, or the canonical CI caller. Keeping
`tochi-mba/LUCY-assistant@v1` is deliberate: the broker trusts that public workflow's
OIDC identity. `--keep-sources` retains upstream client URLs when your account can still
read them; `--self-host-ci` is only for a deployment operating its own app and broker.
Relock on a machine signed in to the destination owner and push the resulting changes.

Sign in with `gh auth login` as the destination owner. Local `make run` / `make up` use
that login only. For CI, run `python scripts/connect_github.py` there: the browser opens
the **lucy-assistant family CI** Install page, you click Install on *your* repositories,
and the broker scopes each job's token to your installation. Your copy may remain
private; its callers continue using the canonical public workflow. The
[sign-in and rollout guide](docs/private-repos.md) has the order.

## Links

| | |
| --- | --- |
| Family standard | [CONTRIBUTING.md](CONTRIBUTING.md) |
| The `lucy` command | [docs/cli.md](docs/cli.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| How Lucy's context is built | [docs/context.md](docs/context.md) |
| Model providers and keys | [docs/models.md](docs/models.md) |
| Sessions and the one write path | [docs/sessions.md](docs/sessions.md) |
| Prompt sections | [docs/prompts.md](docs/prompts.md) |
| Memory as the model sees it | [docs/memory.md](docs/memory.md) |
| Security | [docs/security.md](docs/security.md) |
| CI caller | [docs/ci.md](docs/ci.md) |
| GitHub sign-in and private copies | [docs/private-repos.md](docs/private-repos.md) |
| Adding a service | [docs/adding-a-service.md](docs/adding-a-service.md) |
| Example API | [examples/hello-api](examples/hello-api) |
| ADRs | [docs/adr/README.md](docs/adr/README.md) |
| Parity checker | `python scripts/parity.py` |
| Shared clients | `Keyring-api/clients/python`, `Settings-api/clients/python` |

## Support matrix

| Platform | Services | Compose | Notes |
| --- | --- | --- | --- |
| Linux | Hub and all public siblings | Expected to work | Environments-api sandbox tiers need privileges / `unshare`. |
| macOS | Hub and public siblings; Environments-api directory tier only | Expected to work | Namespace/user tiers are Linux. |
| Windows via WSL2 | Same as Linux | Expected to work | Preferred Windows path. |
| Windows via [devcontainer](.devcontainer/devcontainer.json) | Same as Linux | Expected to work | Docker-in-Docker plus Playwright libraries. |
| Native Windows | Hub and public siblings except Environments-api | Requires Docker Desktop's Linux engine | Run Make recipes in Git Bash. Environments-api runs in Linux containers. |

`--check` on bootstrap marks Environments-api **needs Linux** when the host is not Linux,
rather than running a suite that cannot pass.
