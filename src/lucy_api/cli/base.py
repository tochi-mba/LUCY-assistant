"""The pieces every `lucy` command shares: where to write, where the hub is, how to fail.

Kept apart from the commands themselves so that a command can be added without importing
the parser that will eventually call it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import urlsplit

from lucy_api.cli.config import Config, ConfigError, config_path, load_config

if TYPE_CHECKING:
    import argparse


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
ASCII_SPACE = 32
ASCII_LAST = 126


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

    def warn(self, text: str) -> str:
        return self(text, "33")

    def dim(self, text: str) -> str:
        return self(text, "2")


def wants_colour(stream: TextIO, *, no_color: bool, environ: dict[str, str]) -> bool:
    """Every reason to turn colour off, in the order people expect them honoured."""
    if no_color or environ.get("NO_COLOR") is not None or environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def is_terminal(stream: TextIO | None) -> bool:
    """Whether a question can be asked here at all."""
    return bool(stream is not None and getattr(stream, "isatty", lambda: False)())


def resolve_url(flag: str | None, environ: dict[str, str], config: Config | None = None) -> str:
    """Flag, then environment, then the config file, then this machine.

    An address without a scheme is rejected here rather than at the socket, because the
    failure a connection reports for `box:8000` is "cannot reach Lucy", and the advice that
    comes with unreachable -- start the hub -- is advice that cannot work. This message
    carries its own fix, which is why it is one of the few errors with no separate hint.
    """
    configured = config.get("url") if config else ""
    url = (flag or environ.get(URL_VAR, "").strip() or configured or DEFAULT_URL).rstrip("/")
    if not url.startswith(("http://", "https://")):
        message = "a hub address must start with http:// or https://"
        raise CliError(message, USAGE)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        message = "the hub address has an invalid host or port"
        raise CliError(message, USAGE) from exc
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or port == 0
        or any(character.isspace() or ord(character) < ASCII_SPACE for character in url)
    ):
        message = "use a hub URL with a host and no credentials, query, fragment or whitespace"
        raise CliError(message, USAGE)
    return url


def resolve_token(environ: dict[str, str], config: Config | None = None) -> str:
    """The environment first, so a pipeline is never at the mercy of a saved file."""
    token = environ.get(TOKEN_VAR, "").strip() or (config.get("token") if config else "")
    if any(not ASCII_SPACE < ord(character) <= ASCII_LAST for character in token):
        message = "the token must be a single line of printable ASCII with no spaces"
        raise CliError(message, USAGE)
    return token


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch(client: Any, url: str, path: str, token: str) -> Any:
    """One GET. An unreachable hub is a different answer from a hub that said no."""
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    try:
        return client.get(f"{url}{path}", headers=headers(token))
    except httpx.HTTPError as exc:
        message = f"cannot reach Lucy at {url}"
        hint = (
            f"start it with `lucy serve`, or set {URL_VAR} to where it runs "
            f"({exc.__class__.__name__})"
        )
        raise CliError(message, UNREACHABLE, hint=hint) from exc


class Context:
    """What a command is handed: where to write, where the hub is, and how to say it."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        out: TextIO,
        err: TextIO,
        environ: dict[str, str],
        in_: TextIO | None = None,
    ) -> None:
        self.args = args
        self.out = out
        self.err = err
        self.in_ = in_
        self.environ = environ
        try:
            self.config = load_config(environ)
        except ConfigError as exc:
            if args.command == "setup" and args.force:
                self.config = Config(config_path(environ), {}, (), True)
            else:
                hint = "fix the file by hand, or overwrite it with `lucy setup --force`"
                raise CliError(str(exc), USAGE, hint=hint) from exc
        self.url = resolve_url(args.url, environ, self.config)
        # Saved credentials belong to the hub named beside them. A one-off URL override
        # must not send that token to a different server.
        bound_config = self.config if self.url == self.config.get("url").rstrip("/") else None
        self.token = resolve_token(environ, bound_config)
        self.style = Style(enabled=wants_colour(out, no_color=args.no_color, environ=environ))

    @property
    def interactive(self) -> bool:
        """A question may only be asked where there is somebody to answer it."""
        return is_terminal(self.in_) and is_terminal(self.err) and not self.args.json

    def emit(self, payload: object, text: str) -> None:
        """The answer goes to stdout; `--json` is the shape a script should depend on."""
        if self.args.quiet and not self.args.json:
            return
        print(json.dumps(payload, indent=2) if self.args.json else text, file=self.out)

    def say(self, text: str = "") -> None:
        """Commentary, progress and prompts. Never the answer, so never stdout."""
        if not self.args.quiet and not self.args.json:
            print(text, file=self.err)
