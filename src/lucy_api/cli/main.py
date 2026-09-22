"""`lucy` — the command-line client.

The contract, because a CLI is a user interface *and* an API for scripts:

* **stdout** carries the answer. **stderr** carries everything else — commentary, prompts,
  errors — so a pipe gets only the answer and a person still sees the explanation.
* **Exit codes** are the script's version of the answer: ``0`` it worked, ``1`` the hub
  answered and the answer was no, ``2`` the command was wrong (argparse owns this one),
  ``3`` the hub could not be reached, ``130`` you pressed Ctrl-C.
* **`--json`** makes any command machine-readable, failures included: an error becomes a
  JSON object on stderr rather than a sentence. The text form may be reworded; the JSON is
  a contract.
* **Colour** is off when stdout is not a terminal, when ``NO_COLOR`` is set, when ``TERM``
  is ``dumb``, or when ``--no-color`` is passed.
* **Secrets never arrive as flags** — a flag lands in shell history and in `ps`. The token
  comes from ``LUCY_TOKEN`` or from the file `lucy setup` writes.
* **Settings resolve flag, then environment, then the config file, then the default**,
  which is the order people expect and the order that makes a one-off override easy.
* **Nothing prompts unless somebody is there to answer.** A command run from a script with
  no terminal says which flag to pass instead of hanging on a question nobody will read.

Startup is kept quick by importing `httpx`, `uvicorn` and the settings model inside the
commands that need them, so `lucy --help` does not pay for a network stack it will not use.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, TextIO

from lucy_api import __version__
from lucy_api.cli.base import (
    DEFAULT_URL,
    DOCS,
    FAMILY_ROOT_VAR,
    HTTP_OK,
    INTERRUPTED,
    OK,
    REFUSED,
    TIMEOUT_SECONDS,
    TOKEN_VAR,
    URL_VAR,
    USAGE,
    CliError,
    Context,
    Style,
    fetch,
    wants_colour,
)
from lucy_api.cli.config import CONFIG_VAR
from lucy_api.cli.connect import cmd_connect
from lucy_api.cli.models import cmd_models
from lucy_api.cli.setup import cmd_config, cmd_doctor, cmd_setup
from lucy_api.cli.talk import cmd_talk

if TYPE_CHECKING:
    from collections.abc import Sequence

EPILOG = f"""\
examples:
  lucy setup                     first run: choose how Lucy runs, and sign in
  lucy setup --mode family       bootstrap the family and install the CI GitHub App
  lucy status                    is the hub alive, ready, and who am I
  lucy status --json             the same, for a script
  lucy doctor                    why isn't this working
  lucy connect music             set up one capability, or change it later
  lucy models                    every model provider: ready, configured, or how to set it up
  lucy models connect groq       save a provider key; the key is prompted, never a flag
  lucy talk Hello                one message; the reply is on stdout
  lucy talk                      type interactively, or pipe a message
  lucy serve                     run the hub here, in the foreground
  LUCY_URL=http://box:8000 lucy status    ask a hub somewhere else

environment:
  {URL_VAR}      where the hub is (default: {DEFAULT_URL})
  {TOKEN_VAR}    your keyring token. Never pass a token as a flag.
  {CONFIG_VAR}   where `lucy setup` keeps its answers
  {FAMILY_ROOT_VAR}  family checkout, if it is not this directory
  NO_COLOR     set to anything to turn colour off

exit codes:
  0 it worked   1 the answer was no   2 bad command   3 hub unreachable

docs: {DOCS}
"""


def cmd_status(ctx: Context) -> int:
    """Is the hub alive, is it ready, and who does it think I am."""
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    url = ctx.url
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        alive = fetch(client, url, "/healthy", ctx.token)
        ready = fetch(client, url, "/ready", ctx.token)
        me = fetch(client, url, "/v1/me", ctx.token)

    ready_body = _json_object(ready) if _is_json(ready) else {}
    checks = ready_body.get("checks", {})
    if not isinstance(checks, dict):
        checks = {}
    account = _json_object(me).get("account_id") if me.status_code == HTTP_OK else None
    account = account if isinstance(account, str) else None
    payload = {
        "url": url,
        "alive": alive.status_code == HTTP_OK,
        "ready": ready.status_code == HTTP_OK,
        "checks": {
            name: check.get("status") if isinstance(check, dict) else "unknown"
            for name, check in checks.items()
        },
        "account_id": account,
    }

    mark = ctx.style.good("yes") if payload["ready"] else ctx.style.bad("no")
    lines = [
        f"hub     {url}",
        f"alive   {ctx.style.good('yes') if payload['alive'] else ctx.style.bad('no')}",
        f"ready   {mark}",
    ]
    lines.extend(f"  {name:<10} {status}" for name, status in sorted(payload["checks"].items()))
    if payload["account_id"]:
        lines.append(f"you     {payload['account_id']}")
    elif ctx.token:
        reason = (
            "your token was refused"
            if me.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
            else "identity could not be checked; run lucy doctor"
        )
        lines.append(f"you     {ctx.style.bad('not identified')} — {reason}")
    else:
        lines.append(f"you     not signed in {ctx.style.dim('(run `lucy setup`)')}")
    if not payload["ready"]:
        lines.append(ctx.style.dim("a dependency is unusable; the checks above say which"))

    ctx.emit(payload, "\n".join(lines))
    return OK if payload["alive"] and payload["ready"] and (not ctx.token or account) else REFUSED


def cmd_version(ctx: Context) -> int:
    """The client's version, and the hub's when one answers."""
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    hub: str | None = None
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = fetch(client, ctx.url, "/healthy", ctx.token)
        if response.status_code == HTTP_OK:
            version = _json_object(response).get("version")
            hub = version if isinstance(version, str) else None
    except CliError:
        hub = None
    ctx.emit(
        {"client": __version__, "hub": hub},
        f"lucy {__version__} {ctx.style.dim(f'(hub: {hub or "not running"})')}",
    )
    return OK


def cmd_serve(ctx: Context) -> int:
    """Run the hub in the foreground. For development; compose is how it is deployed.

    A flag given here becomes an environment variable, because that is the only channel the
    server reads. A flag *not* given sets nothing, so `LUCY_PORT` and the `.env` file still
    decide — overwriting them with argparse's defaults would silently ignore the
    configuration the person already wrote down.
    """
    from lucy_api.__main__ import main as serve  # noqa: PLC0415 - uvicorn is a heavy import
    from lucy_api.core.config import load_settings  # noqa: PLC0415 - so is pydantic-settings

    for variable, value in (("LUCY_HOST", ctx.args.host), ("LUCY_PORT", ctx.args.port)):
        if value is not None:
            os.environ[variable] = str(value)
            ctx.environ[variable] = str(value)
    try:
        settings = load_settings()
    except (RuntimeError, ValueError) as exc:
        hint = "check your .env file and any LUCY_* variables in this shell"
        message = "the hub's configuration is not usable"
        raise CliError(message, USAGE, hint=hint) from exc

    ctx.say(f"Lucy is starting on http://{settings.host}:{settings.port}  (Ctrl-C to stop)")
    serve()
    return OK


def _is_json(response: object) -> bool:
    headers = getattr(response, "headers", {})
    return str(headers.get("content-type", "")).startswith("application/json")


def _json_object(response: Any) -> dict[str, Any]:
    """A proxy's HTML or a broken payload must not crash diagnostics."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _report(
    exc: CliError, *, args: argparse.Namespace, err: TextIO, environ: dict[str, str]
) -> None:
    """A failure, said once, in whichever language was asked for.

    It goes to stderr either way, so stdout still carries only the answer and a pipe is
    never handed an error object where it expected a result. `--json` is a contract, and a
    contract that covers only the happy path leaves a script with an exit code and nothing
    to log.
    """
    if args.json:
        error = {"error": {"message": str(exc), "hint": exc.hint, "exit_code": exc.code}}
        print(json.dumps(error, indent=2), file=err)
        return
    style = Style(enabled=wants_colour(err, no_color=args.no_color, environ=environ))
    print(f"{style.bad('lucy:')} {exc}", file=err)
    if exc.hint:
        print(f"  {style.dim(exc.hint)}", file=err)


def _shared_flags(parser: argparse.ArgumentParser, *, keep_defaults: bool) -> None:
    """The flags that have to work on both sides of the subcommand.

    People type ``lucy status --json``; a generated script emits ``lucy --json status``.
    argparse only recognises a flag where it was declared, so these are declared twice: once
    on the root with real defaults, and once on a parent every subcommand inherits. The
    second copy defaults to ``SUPPRESS``, because argparse copies a subcommand's whole
    namespace over the root's, and a plain ``False`` there would erase a ``--json`` that was
    passed before the subcommand.
    """
    hidden: dict[str, Any] = {} if keep_defaults else {"default": argparse.SUPPRESS}
    parser.add_argument(
        "-V", "--version", action="store_true", help="show versions and exit", **hidden
    )
    parser.add_argument(
        "--url", metavar="URL", help=f"where the hub is (default: ${URL_VAR})", **hidden
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output", **hidden)
    parser.add_argument("--no-color", action="store_true", help="never colour the output", **hidden)
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="print nothing on success", **hidden
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lucy",
        description="Talk to Lucy from anywhere.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _shared_flags(parser, keep_defaults=True)

    after = argparse.ArgumentParser(add_help=False)
    _shared_flags(after, keep_defaults=False)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    setup = sub.add_parser("setup", parents=[after], help="first run: set Lucy up on this machine")
    setup.add_argument("--mode", choices=("hub", "family", "remote"), help="how Lucy runs")
    credentials = setup.add_mutually_exclusive_group()
    credentials.add_argument(
        "--token-stdin", action="store_true", help="read the token from standard input"
    )
    credentials.add_argument("--no-token", action="store_true", help="do not save a token")
    setup.add_argument("--capabilities", action="store_true", help="also offer each capability")
    setup.add_argument("-y", "--yes", action="store_true", help="take every default, ask nothing")
    setup.add_argument("--force", action="store_true", help="overwrite an existing config")
    setup.add_argument("--dry-run", action="store_true", help="say what would change, change none")
    github = setup.add_mutually_exclusive_group()
    github.add_argument(
        "--github-ci",
        action="store_true",
        help="install the family CI GitHub App (opens the browser)",
    )
    github.add_argument(
        "--no-github-ci",
        action="store_true",
        help="skip the family CI GitHub App install",
    )
    setup.set_defaults(run=cmd_setup)

    connect = sub.add_parser("connect", parents=[after], help="set up one capability")
    connect.add_argument("capability", nargs="?", help="which one; omit to list them")
    connect.add_argument("-y", "--yes", action="store_true", help="take every default")
    connect.add_argument("--dry-run", action="store_true", help="say what would change")
    connect.set_defaults(run=cmd_connect)

    status = sub.add_parser("status", parents=[after], help="is the hub alive, ready, and who am I")
    status.set_defaults(run=cmd_status)

    doctor = sub.add_parser("doctor", parents=[after], help="why isn't this working")
    doctor.set_defaults(run=cmd_doctor)

    config = sub.add_parser("config", parents=[after], help="what is configured, and from where")
    config.set_defaults(run=cmd_config)

    version = sub.add_parser("version", parents=[after], help="the client's version, and the hub's")
    version.set_defaults(run=cmd_version)

    serve = sub.add_parser("serve", parents=[after], help="run the hub here, in the foreground")
    serve.add_argument("--host", metavar="HOST", help="default: $LUCY_HOST, else 127.0.0.1")
    serve.add_argument("--port", type=int, metavar="PORT", help="default: $LUCY_PORT, else 8000")
    serve.set_defaults(run=cmd_serve)

    talk = sub.add_parser("talk", parents=[after], help="send a message and print the reply")
    talk.add_argument("-s", "--session", metavar="ID", help="continue this conversation")
    talk.add_argument("words", nargs="*", help="the message; omit to read stdin")
    talk.set_defaults(run=cmd_talk)

    models = sub.add_parser("models", parents=[after], help="which models you can use")
    models.add_argument("--check", action="store_true", help="prove the configured keys now")
    models.set_defaults(run=cmd_models)
    models_sub = models.add_subparsers(dest="models_command", metavar="<action>")
    connect_model = models_sub.add_parser(
        "connect", parents=[after], help="give the hub a key for one provider"
    )
    connect_model.add_argument("provider", help="the provider id; `lucy models` lists them")
    connect_model.set_defaults(run=cmd_models)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    in_: TextIO | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr
    stdin = in_ if in_ is not None else sys.stdin
    env = environ if environ is not None else dict(os.environ)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        args.run = cmd_version
    if getattr(args, "run", None) is None:
        parser.print_help(stdout)
        return OK

    try:
        ctx = Context(args, out=stdout, err=stderr, in_=stdin, environ=env)
        return int(args.run(ctx))
    except CliError as exc:
        _report(exc, args=args, err=stderr, environ=env)
        return exc.code
    except KeyboardInterrupt:
        # Ctrl-C is an answer, not a crash. A blank line first, because the cursor is
        # sitting at the end of whatever was half-printed when the signal arrived.
        print(file=stderr)
        return INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
