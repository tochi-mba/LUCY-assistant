# The `lucy` command

`lucy` is a **client**. It talks to a running hub over HTTP, so the same command works
whether Lucy is on this laptop, in compose, or on a machine down the hall. The only thing
that changes is `LUCY_URL`.

`lucy serve` is the one exception: it runs the hub here, in the foreground, for development.
Deployments use compose.

## Install it globally

From a checkout, the first-run scripts install the client and then launch its setup guide.
They resolve their own directory, so you can run them from anywhere:

```powershell
pwsh C:\path\to\LUCY-assistant\scripts\setup.ps1
```

```bash
bash /path/to/LUCY-assistant/scripts/setup.sh
```

Both require `uv`; a missing installation gets an installation link and a nonzero exit.
`--dry-run` previews without installing or saving anything. `--skip-setup` installs only.
PowerShell also accepts `-DryRun` and `-SkipSetup`. Other options are passed to `lucy setup`.
The scripts do not start services or replace conflicting executables with `--force`.

Installing the wheel as a **uv tool** puts `lucy` on your PATH, in its own isolated
environment, so it works from any directory and cannot collide with a project's
dependencies.

```bash
uv tool list | grep lucy-api        # already installed? then skip the next line
uv tool install --editable .        # from this repository
uv tool update-shell                # once per machine, if uv says PATH needs it
```

`--editable` points the installed command at this checkout, so `git pull` updates the
command with no reinstall. Drop it for a fixed copy, and re-run `uv tool install --force`
to move a fixed copy forward.

```bash
uv tool upgrade lucy-api            # a non-editable install
uv tool uninstall lucy-api          # remove it
```

Working inside the repository, `uv run lucy …` needs no install at all.

## Commands

| | |
| --- | --- |
| `lucy setup` | Choose a hub, save client preferences, optionally save a token, and show what is already in place. |
| `lucy config` | Show effective values and where they came from; redact the token. |
| `lucy doctor` | Check the local installation, hub readiness and caller identity. |
| `lucy connect [capability]` | List setup requirements, or show instructions for one capability. |
| `lucy models [--check]` | Every model provider the hub knows: ready, configured but unproven, or what would make it usable. `--check` proves configured keys now. |
| `lucy models connect <provider>` | Save a provider key into the family `.env`. The key is prompted, never a flag. See [models.md](models.md). |
| `lucy status` | Is the hub alive, is it ready, which dependency is unusable, and who does it think you are. |
| `lucy version` | This client's version, and the hub's when one answers. |
| `lucy talk [words]` | Send a message (or pipe one) and print the reply. `--session` continues. After each reply, stderr says how full the window is (`context 42% · compacts at 72%`), and names any compaction Lucy did on her own. |
| `lucy context [-s ID]` | How full a conversation's window is, in the figure the hub acts on: percent, tokens left before automatic compaction, turns read as a summary. Your latest conversation without `-s`. |
| `lucy compact [-s ID] [--keep N]` | Summarise the older turns now, keeping the newest `N` word for word (default: your `history_turns_kept`). Says the window before and after. Automatic compaction carries on regardless. |
| `lucy uncompact [-s ID] [COMPACTION]` | Put the turns back. Without an id, undoes the compaction Lucy is reading. |
| `lucy eval list` | The regression conversations that ship with Lucy, and what each guards. No hub needed. |
| `lucy eval run --model SPEC` | Hold them against a running hub with a real model and write a report. Never run by CI. See [evals.md](evals.md). |
| `lucy logs` | The hub's log file, filtered: `--session`, `--turn`, `--agent` (any part of an id), `--level`, `--grep`, `--last N`; `--json` for the raw lines; `--file` for a log somewhere other than the family's `var/log/lucy.jsonl`. Reads the file on this machine, so it works when the hub does not. |
| `lucy serve` | Run the hub in the foreground. `--host` and `--port`. |

`lucy` with no command prints help, and exits 0. Help leads with examples, because that is
what people read.

### Commands inside `lucy talk`

At the `you:` prompt, a line that starts with `/` and names a command is for the client, not
for Lucy, and is never sent:

| | |
| --- | --- |
| `/context` | How full this conversation's window is. |
| `/compact [N]` | Compact now, keeping the newest `N` turns. |
| `/uncompact [ID]` | Undo a compaction; the one Lucy is reading by default. |
| `/new` | Start a fresh conversation with your next message. |
| `/session` | Which conversation this is. |
| `/help` | The list. |
| `/quit`, `/exit` | Leave. Ctrl-D does too; a turn still running carries on. |

`//text` sends `/text`. A line that does not look like a command, such as `/etc/hosts is
wrong`, goes to Lucy as written; `/typo` is named as an unknown command rather than sent. A
command that fails says why and the prompt carries on. `lucy talk /context` works one-shot,
on your latest conversation.

## Flags

| | |
| --- | --- |
| `--url URL` | Where the hub is. Overrides `LUCY_URL`. |
| `--json` | Machine-readable output. |
| `--no-color` | Never colour the output. |
| `-q`, `--quiet` | Print nothing on success. `--json` still prints, because a script asked for it. |
| `-V`, `--version` | The same as `lucy version`. |

Every one of these works **on either side of the subcommand**. `lucy status --json` is what
a person types and `lucy --json status` is what a script generator emits, so both are
accepted.

## First run

The interactive guide offers three modes: `hub` for running the hub here, `family` for
running the family from a checkout, and `remote` for an existing hub. These are saved
preferences and next-step instructions; choosing a mode does not start containers, install
dependencies or create an account.

Every run starts with what is already in place -- the saved configuration, the hub, the
token, the family checkout, the family CI GitHub App and any private extras -- each marked
`done` or `next`. `family` mode also opens the
[family CI app's](private-repos.md#connect-ci-one-command-one-install-click) Install page
unless it is installed already; `--no-github-ci` skips that, and `--github-ci` asks for it in
any mode, even when it is installed. On a machine that is already set up, a run with no
flags keeps the saved answers unless you say, when asked, that you want to change them.

```bash
lucy setup --mode remote --url https://lucy.example --no-token --yes
lucy setup --dry-run --json
lucy config
lucy doctor
```

Setup asks questions only with interactive input and stderr, and never with `--json`.
Without a terminal, provide `--mode` or `--yes`. Remote setup needs a URL. An existing
file is preserved unless you confirm interactively or pass `--force`; `--yes` alone does
not overwrite it. `--force` also permits replacing a malformed configuration.

The hidden token prompt is optional. `--token-stdin` accepts a token from a pipe;
`--no-token` omits or removes the saved token. These options are mutually exclusive.
An environment token is never silently copied into the saved file. A dry run never reads
the token input, writes a file or contacts the hub. Ctrl-C leaves any uncompleted write
uncommitted; completed configuration remains available if later discovery fails.

The file lives at `$XDG_CONFIG_HOME/lucy/config.toml`, otherwise
`%APPDATA%/lucy/config.toml` on Windows or `~/.config/lucy/config.toml` elsewhere.
`LUCY_CONFIG` selects a different file. Writes are atomic and owner-only on POSIX;
Windows uses the configuration directory's inherited ACL. Do not put the file in a
shared directory. Saved credentials are bound to the saved hub URL: overriding that URL
does not forward the saved token to another server.

## Capability setup

`lucy setup --capabilities` reads the hub's authenticated `/v1/setup` catalogue after
saving the client configuration. A discovery failure returns nonzero with the saved
configuration intact. You can also inspect it later:

```bash
lucy connect
lucy connect music
lucy connect research --json
```

Readiness describes the deployment. It does **not** prove that your account has connected
music or a model provider. The catalogue states account connection status separately:
`not_required`, `connected`, `disconnected`, `pending`, or `unknown` when the vault
could not be read this request. Optional services can be skipped.
The current catalogue uses explicit adapters to the services' readiness routes and setup
documentation; the sibling services do not yet publish their own setup manifests.

Interactive setup signs in with a short-lived device code. Lucy prints the code and how to
approve it, opens the hub's `/device` page (which says the same and asks for nothing), and
polls at the interval the hub gave it. The approval comes from a client that is already
signed in as you: `lucy approve ABCD-EFGH` there hands its sign-in to the waiting one, and
`--deny` refuses it. No step asks for a password. With no other client signed in, a first
sign-in uses `--token-stdin` with a token from your keyring. The resulting `aud=lucy-api`
token is written only to the private configuration file. `--yes` never opens a browser.

Music connections use Lucy's `/v1/connections` surface over Keyring's delegated boundary.
Provider credentials remain in Keyring. A named `connect` command returns 1 while setup
still needs attention. `--dry-run` performs no connection mutation.

## Environment

| | |
| --- | --- |
| `LUCY_URL` | Where the hub is. Default `http://127.0.0.1:8000`. |
| `LUCY_TOKEN` | Your keyring token, audience `lucy-api`. |
| `LUCY_CONFIG` | Override the client configuration file location. |
| `LUCY_FAMILY_ROOT` | The family checkout, when it is not this directory or one above it. `lucy setup`, `lucy logs` and `lucy models connect` look for it. |
| `NO_COLOR` | Set to anything to turn colour off. So does `TERM=dumb`. |

**A token is never a flag.** A flag lands in shell history and in the output of `ps`, where
anybody on the machine can read it. Use browser sign-in, stdin, or the existing
`LUCY_TOKEN` override. Configuration inspection shows only a redacted token.

An address is resolved **flag, then environment, then config file, then this machine**. An address with no
scheme is refused before the socket, because "cannot reach Lucy" would be the wrong
diagnosis and "start the hub" would be advice that cannot work.

## Output

**stdout carries the answer and stderr carries everything else**, so a pipe gets only the
answer while a person still sees the explanation.

`--json` produces structured command results and runtime failures. Argument-parser usage
errors and `--help` retain argparse's text format. Text descriptions may be reworded.

```bash
lucy status --json | jq -e '.ready' >/dev/null && echo "good to go"
```

Failures are machine-readable too. Under `--json` an error is a JSON object on **stderr**,
so stdout is never handed an error where it expected a result:

```json
{ "error": { "message": "cannot reach Lucy at http://127.0.0.1:8000",
             "hint": "start it with `lucy serve`, or set LUCY_URL to where it runs",
             "exit_code": 3 } }
```

`hint` is always present and is `null` when the message already carries its own fix.

Colour is on only when stdout is a terminal, and off for `NO_COLOR`, `TERM=dumb` and
`--no-color`.

## Exit codes

| | |
| ---: | --- |
| `0` | It worked. |
| `1` | The hub answered and the answer was no. A dependency is unusable. |
| `2` | The command was wrong. Also argparse's own usage errors. |
| `3` | The hub could not be reached. |
| `130` | You pressed Ctrl-C. |

`1` and `3` are deliberately different. A script that retries should retry on `3` and
report on `1`, and collapsing the two makes an outage indistinguishable from a refusal.

## When it does not work

| | |
| --- | --- |
| `lucy: command not found` | `uv tool update-shell`, then open a new terminal. |
| `cannot reach Lucy at …` | Nothing is listening. `lucy serve`, or `make up`, or set `LUCY_URL`. |
| `ready no` with a named check | That dependency is down. `lucy status` names which one. |
| `not signed in` | Run `lucy setup --force` to save a current token, or set `LUCY_TOKEN`. |
| `your token was refused` | The token is expired, or its audience is not `lucy-api`. |

The interface follows the [Command Line Interface Guidelines](https://clig.dev/):
examples in help, explicit noninteractive options, stdout for results, stderr for prompts,
and credentials kept out of flags.
