"""`lucy` — the command-line client.

The contract, because a CLI is a user interface *and* an API for scripts:

* **stdout** carries the answer. **stderr** carries everything else, so a pipe gets only
  the answer and a person still sees the explanation.
* **Exit codes** are the script's version of the answer: ``0`` it worked, ``1`` the hub
  answered and the answer was no, ``2`` the command was wrong (argparse owns this one),
  ``3`` the hub could not be reached, ``130`` you pressed Ctrl-C.
* **`--json`** makes any command machine-readable, failures included: an error becomes a
  JSON object on stderr rather than a sentence. The text form may be reworded; the JSON is
  a contract.
* **Colour** is off when stdout is not a terminal, when ``NO_COLOR`` is set, when ``TERM``
  is ``dumb``, or when ``--no-color`` is passed.
* **Secrets never arrive as flags** — a flag lands in shell history and in `ps`. The token
  comes from ``LUCY_TOKEN``.
* **Settings resolve flag, then environment, then the default**, which is the order people
  expect and the order that makes a one-off override easy.

Startup is kept quick by importing `httpx` and `uvicorn` inside the commands that need
them, so `lucy --help` does not pay for a network stack it will not use.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import TYPE_CHECKING, Any, TextIO

from lucy_api import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_URL = "http://127.0.0.1:8000"
URL_VAR = "LUCY_URL"
TOKEN_VAR = "LUCY_TOKEN"  # noqa: S105 - the variable's name, not a token
DOCS = "https://github.com/tochi-mba/LUCY-assistant"

OK = 0
REFUSED = 1
USAGE = 2
UNREACHABLE = 3
INTERRUPTED = 130

TIMEOUT_SECONDS = 10.0
HTTP_OK = 200

EPILOG = f"""\
examples:
  lucy status                    is the hub alive, ready, and who am I
  lucy status --json             the same, for a script
  lucy serve                     run the hub here, in the foreground
  LUCY_URL=http://box:8000 lucy status    ask a hub somewhere else

environment:
  {URL_VAR}      where the hub is (default: {DEFAULT_URL})
  {TOKEN_VAR}    your keyring token. Never pass a token as a flag.
  NO_COLOR     set to anything to turn colour off

exit codes:
  0 it worked   1 the answer was no   2 bad command   3 hub unreachable

docs: {DOCS}
"""


class CliError(Exception):
    """A failure whose message is already a sentence a person can act on."""

    def __init__(self, message: str, code: int = REFUSED, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


class Style:
    """Colour, when the terminal wants it and the person has not said otherwise."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def good(self, text: str) -> str:
        return self(text, "32")

    def bad(self, text: str) -> str:
        return self(text, "31")

    def dim(self, text: str) -> str:
        return self(text, "2")


def wants_colour(stream: TextIO, *, no_color: bool, environ: dict[str, str]) -> bool:
    """Every reason to turn colour off, in the order people expect them to be honoured."""
    if no_color or environ.get("NO_COLOR") is not None or environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def resolve_url(flag: str | None, environ: dict[str, str]) -> str:
    """Flag, then environment, then this machine.

    An address without a scheme is rejected here rather than at the socket, because the
    failure a connection reports for `box:8000` is "cannot reach Lucy", and the advice that
    comes with it -- start the hub -- is advice that cannot work. This message carries its
    own fix, which is why it is one of the few errors with no separate hint.
    """
    url = (flag or environ.get(URL_VAR) or DEFAULT_URL).rstrip("/")
    if not url.startswith(("http://", "https://")):
        message = f"a hub address must start with http:// or https:// -- got {url!r}"
        raise CliError(message, USAGE)
    return url


def _headers(environ: dict[str, str]) -> dict[str, str]:
    token = environ.get(TOKEN_VAR, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch(client: Any, url: str, path: str, environ: dict[str, str]) -> Any:
    """One GET. An unreachable hub is a different answer from a hub that said no."""
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    try:
        return client.get(f"{url}{path}", headers=_headers(environ))
    except httpx.HTTPError as exc:
        message = f"cannot reach Lucy at {url}"
        hint = (
            f"start it with `lucy serve`, or set {URL_VAR} to where it runs "
            f"({exc.__class__.__name__})"
        )
        raise CliError(message, UNREACHABLE, hint=hint) from exc


def cmd_status(ctx: Context) -> int:
    """Is the hub alive, is it ready, and who does it think I am."""
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    url = ctx.url
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        alive = fetch(client, url, "/healthy", ctx.environ)
        ready = fetch(client, url, "/ready", ctx.environ)
        me = fetch(client, url, "/v1/me", ctx.environ)

    checks = ready.json().get("checks", {}) if _is_json(ready) else {}
    payload = {
        "url": url,
        "alive": alive.status_code == HTTP_OK,
        "ready": ready.status_code == HTTP_OK,
        "checks": {name: check.get("status") for name, check in checks.items()},
        "account_id": me.json().get("account_id") if me.status_code == HTTP_OK else None,
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
    elif ctx.environ.get(TOKEN_VAR):
        lines.append(f"you     {ctx.style.bad('not identified')} — {TOKEN_VAR} was refused")
    else:
        lines.append(f"you     not signed in {ctx.style.dim(f'(set {TOKEN_VAR})')}")
    if not payload["ready"]:
        lines.append(ctx.style.dim("a dependency is unusable; the checks above say which"))

    ctx.emit(payload, "\n".join(lines))
    return OK if payload["ready"] else REFUSED


def cmd_version(ctx: Context) -> int:
    """The client's version, and the hub's when one answers."""
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    hub: str | None = None
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = fetch(client, ctx.url, "/healthy", ctx.environ)
        if response.status_code == HTTP_OK:
            hub = response.json().get("version")
    except CliError:
        hub = None
    ctx.emit(
        {"client": __version__, "hub": hub},
        f"lucy {__version__} {ctx.style.dim(f'(hub: {hub or "not running"})')}",
    )
    return OK


def cmd_serve(ctx: Context) -> int:
    """Run the hub in the foreground. For development; compose is how it is deployed."""
    from lucy_api.__main__ import main as serve  # noqa: PLC0415 - uvicorn is a heavy import

    host, port = ctx.args.host, ctx.args.port
    ctx.environ.setdefault("LUCY_HOST", host)
    ctx.environ.setdefault("LUCY_PORT", str(port))
    os.environ.update({"LUCY_HOST": host, "LUCY_PORT": str(port)})
    print(f"Lucy is starting on http://{host}:{port}  (Ctrl-C to stop)", file=ctx.err)
    serve()
    return OK


def _is_json(response: Any) -> bool:
    return str(response.headers.get("content-type", "")).startswith("application/json")


class Context:
    """What a command is handed: where to write, where the hub is, and how to say it."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        out: TextIO,
        err: TextIO,
        environ: dict[str, str],
    ) -> None:
        self.args = args
        self.out = out
        self.err = err
        self.environ = environ
        self.url = resolve_url(args.url, environ)
        self.style = Style(enabled=wants_colour(out, no_color=args.no_color, environ=environ))

    def emit(self, payload: object, text: str) -> None:
        """The answer goes to stdout; `--json` is the shape a script should depend on."""
        if self.args.quiet and not self.args.json:
            return
        print(json.dumps(payload, indent=2) if self.args.json else text, file=self.out)


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
        "--url", metavar="URL", help=f"where the hub is (default: ${URL_VAR})", **hidden
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output", **hidden)
    parser.add_argument("--no-color", action="store_true", help="never colour the output", **hidden)
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="print nothing on success", **hidden
    )


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lucy",
        description="Talk to Lucy from anywhere.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="store_true", help="show versions and exit")
    _shared_flags(parser, keep_defaults=True)

    after = argparse.ArgumentParser(add_help=False)
    _shared_flags(after, keep_defaults=False)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    status = sub.add_parser("status", parents=[after], help="is the hub alive, ready, and who am I")
    status.set_defaults(run=cmd_status)

    version = sub.add_parser("version", parents=[after], help="the client's version, and the hub's")
    version.set_defaults(run=cmd_version)

    serve = sub.add_parser("serve", parents=[after], help="run the hub here, in the foreground")
    serve.add_argument("--host", default="127.0.0.1", metavar="HOST")
    serve.add_argument("--port", type=int, default=8000, metavar="PORT")
    serve.set_defaults(run=cmd_serve)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr
    env = environ if environ is not None else dict(os.environ)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        args.run = cmd_version
    if getattr(args, "run", None) is None:
        parser.print_help(stdout)
        return OK

    try:
        ctx = Context(args, out=stdout, err=stderr, environ=env)
        return int(args.run(ctx))
    except CliError as exc:
        _report(exc, args=args, err=stderr, environ=env)
        return exc.code
    except KeyboardInterrupt:  # pragma: no cover - a signal, not a branch
        print(file=stderr)
        return INTERRUPTED


if __name__ == "__main__":  # pragma: no cover - the console script calls main()
    sys.exit(main())
