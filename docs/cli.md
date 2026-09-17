# The `lucy` command

`lucy` is a **client**. It talks to a running hub over HTTP, so the same command works
whether Lucy is on this laptop, in compose, or on a machine down the hall. The only thing
that changes is `LUCY_URL`.

`lucy serve` is the one exception: it runs the hub here, in the foreground, for development.
Deployments use compose.

## Install it globally

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
| `lucy status` | Is the hub alive, is it ready, which dependency is unusable, and who does it think you are. |
| `lucy version` | This client's version, and the hub's when one answers. |
| `lucy serve` | Run the hub in the foreground. `--host` and `--port`. |

`lucy` with no command prints help, and exits 0. Help leads with examples, because that is
what people read.

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

## Environment

| | |
| --- | --- |
| `LUCY_URL` | Where the hub is. Default `http://127.0.0.1:8000`. |
| `LUCY_TOKEN` | Your keyring token, audience `lucy-api`. |
| `NO_COLOR` | Set to anything to turn colour off. So does `TERM=dumb`. |

**A token is never a flag.** A flag lands in shell history and in the output of `ps`, where
anybody on the machine can read it. The token comes from the environment, and no command
prints it back.

An address is resolved **flag, then environment, then this machine**. An address with no
scheme is refused before the socket, because "cannot reach Lucy" would be the wrong
diagnosis and "start the hub" would be advice that cannot work.

## Output

**stdout carries the answer and stderr carries everything else**, so a pipe gets only the
answer while a person still sees the explanation.

`--json` is a contract. The text form may be reworded in any release; the JSON keys will
not change without a major version.

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
| `not signed in` | Set `LUCY_TOKEN`. |
| `LUCY_TOKEN was refused` | The token is expired, or its audience is not `lucy-api`. |
