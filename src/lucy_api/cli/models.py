"""`lucy models`: which models you can use, and `lucy models connect` to add one.

The listing is the hub's own answer -- `GET /v1/models` -- rendered in three sections so
the first thing a person sees is what works, and the last thing is what each of the rest
would need. Nothing here guesses at a provider list; a client that shipped its own would
be wrong the week after the hub gained a row.

Connecting a provider means giving the hub a key, and the hub reads its keys from the
environment (`LUCY_MODEL_KEYS`) or the `.env` file beside it. So `connect` writes to that
file, atomically, and never accepts the key as a flag: a flag lands in shell history and in
`ps`, and a key is the one thing that must not.
"""

from __future__ import annotations

import json
import os
import tempfile
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_api.cli.base import OK, REFUSED, TIMEOUT_SECONDS, USAGE, CliError, fetch
from lucy_api.cli.family import PACKAGE_ROOT, find_family_root

if TYPE_CHECKING:
    from lucy_api.cli.base import Context

KEYS_VARIABLE = "LUCY_MODEL_KEYS"
ENV_FILE = ".env"
BOLD = "1"
QUOTED = 2
"""The shortest value that can be wrapped in a pair of quotes."""
SECTIONS = ("ready", "available", "unavailable")
HEADINGS = {
    "ready": "ready -- checked, usable now",
    "available": "available -- configured, not yet checked",
    "unavailable": "unavailable -- and what each one needs",
}


def cmd_models(ctx: Context) -> int:
    """List the catalogue, or connect one provider."""
    if getattr(ctx.args, "models_command", None) == "connect":
        return _connect(ctx)
    return _list(ctx)


# --------------------------------------------------------------------------------------
# lucy models [--check]
# --------------------------------------------------------------------------------------


def _list(ctx: Context) -> int:
    report = _report(ctx, check=bool(getattr(ctx.args, "check", False)))
    lines: list[str] = []
    for section in SECTIONS:
        rows = report.get(section) or []
        if not rows:
            continue
        lines.append(ctx.style(HEADINGS[section], BOLD))
        lines.extend(_line(ctx, section, row) for row in rows)
        lines.append("")
    if not report.get("ready"):
        lines.append("Nothing is ready. Run `lucy models connect <provider>` to add one.")
    ctx.emit(report, "\n".join(lines).rstrip())
    return OK


def _line(ctx: Context, section: str, row: dict[str, Any]) -> str:
    provider = str(row.get("provider", ""))
    title = str(row.get("title", provider))
    detail = str(row.get("detail", ""))
    models = ", ".join(str(model) for model in row.get("models") or [])
    if section == "unavailable":
        setup = row.get("setup") or {}
        command = str(setup.get("command") or "")
        fix = f"  run: {command}" if command else ""
        return f"  {provider:<16} {title} - {ctx.style.dim(detail)}{fix}"
    shown = f" ({models})" if models else ""
    return f"  {ctx.style.good(provider):<16} {title}{shown} - {detail}"


def _report(ctx: Context, *, check: bool) -> dict[str, Any]:
    if not ctx.token:
        message = "not signed in"
        raise CliError(message, REFUSED, hint="run `lucy setup`")
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        response = fetch(
            client, ctx.url, "/v1/models?check=true" if check else "/v1/models", ctx.token
        )
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        message = "the hub refused the token"
        raise CliError(message, REFUSED, hint="run `lucy setup --force` with a current token")
    if response.status_code == HTTPStatus.NOT_FOUND:
        message = "this hub does not list models yet"
        raise CliError(message, REFUSED, hint="update the hub, then run `lucy models`")
    if response.status_code != HTTPStatus.OK:
        message = "the hub cannot list models right now"
        raise CliError(message, REFUSED, hint="run `lucy doctor`")
    try:
        body = response.json()
    except ValueError as exc:
        message = "the hub returned an unreadable model listing"
        raise CliError(message, REFUSED, hint="update the hub and client together") from exc
    if not isinstance(body, dict) or not all(isinstance(body.get(s), list) for s in SECTIONS):
        message = "the hub returned an unreadable model listing"
        raise CliError(message, REFUSED, hint="update the hub and client together")
    return body


# --------------------------------------------------------------------------------------
# lucy models connect <provider>
# --------------------------------------------------------------------------------------


def _connect(ctx: Context) -> int:
    provider = str(ctx.args.provider).strip()  # argparse makes it required
    standing = _standing(ctx, provider)
    setup = standing.get("setup") or {}
    if standing.get("section") == "ready":
        ctx.say(f"{provider} is already connected and answering.")
        return OK
    if not setup.get("command"):
        message = f"{provider} cannot be connected from here"
        raise CliError(message, REFUSED, hint=str(setup.get("instructions") or ""))
    target = _env_file(ctx)
    if standing.get("local"):
        _write_env(target, {KEYS_VARIABLE: _merged(target, provider, "local")})
        ctx.say(f"{provider} switched on. Restart the hub for it to notice.")
        return OK
    key = _read_key(ctx, provider, str(setup.get("console_url") or ""))
    _write_env(target, {KEYS_VARIABLE: _merged(target, provider, key)})
    ctx.say(f"Saved a key for {provider} in {target}. Restart the hub, then `lucy models --check`.")
    return OK


def _standing(ctx: Context, provider: str) -> dict[str, Any]:
    report = _report(ctx, check=False)
    for section in SECTIONS:
        for row in report.get(section) or []:
            if row.get("provider") == provider:
                return dict(row)
    message = f"no model provider called {provider!r}"
    raise CliError(message, USAGE, hint="lucy models   lists every provider the hub knows")


def _read_key(ctx: Context, provider: str, console_url: str) -> str:
    """The key, from a person at a terminal. Never from a flag."""
    if not ctx.interactive or ctx.in_ is None:
        message = f"connecting {provider} needs a terminal to type the key into"
        raise CliError(
            message,
            USAGE,
            hint=f"or set {KEYS_VARIABLE} on the hub's host; a key is never a flag",
        )
    where = f" (create one at {console_url})" if console_url else ""
    print(f"{provider} API key{where}: ", end="", file=ctx.err, flush=True)
    answer = ctx.in_.readline()
    key = answer.strip()
    if not key:
        message = "no key given; nothing was changed"
        raise CliError(message, REFUSED)
    return key


def _env_file(ctx: Context) -> Path:
    """The `.env` the hub reads, which is the one at the family root.

    A hub somewhere else has its own environment, and this command cannot reach it: the
    refusal names the variable so the person can set it where the hub runs.
    """
    root = find_family_root(cwd=Path.cwd(), environ=ctx.environ, origin=PACKAGE_ROOT)
    if root is None:
        message = "cannot find the hub's checkout to write its configuration"
        raise CliError(
            message,
            REFUSED,
            hint=f"set {KEYS_VARIABLE} where the hub runs, or run this from the family checkout",
        )
    return root / ENV_FILE


def _merged(target: Path, provider: str, value: str) -> str:
    """The keys the file already names, plus this one, as the JSON the hub reads."""
    current: dict[str, str] = {}
    existing = _existing_line(target)
    if existing:
        try:
            parsed = json.loads(existing)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            current = {str(k): str(v) for k, v in parsed.items()}
    current[provider] = value
    return json.dumps(current, sort_keys=True)


def _existing_line(target: Path) -> str:
    if not target.is_file():
        return ""
    for line in target.read_text(encoding="utf-8").splitlines():
        name, separator, rest = line.partition("=")
        if separator and name.strip() == KEYS_VARIABLE:
            return _unquote(rest.strip())
    return ""


def _unquote(value: str) -> str:
    if len(value) >= QUOTED and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _write_env(target: Path, values: dict[str, str]) -> None:
    """Replace or append the given variables, atomically, keeping every other line."""
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    remaining = dict(values)
    rendered: list[str] = []
    for line in lines:
        name = line.partition("=")[0].strip()
        if name in remaining:
            rendered.append(f"{name}={_quote(remaining.pop(name))}")
        else:
            rendered.append(line)
    rendered.extend(f"{name}={_quote(value)}" for name, value in remaining.items())
    text = "\n".join(rendered) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        message = f"cannot write {target}: {exc.strerror or exc}"
        raise CliError(message, REFUSED) from exc


def _quote(value: str) -> str:
    """Single-quoted, so the JSON's double quotes survive every dotenv reader."""
    return "'" + value.replace("'", "'\\''") + "'"


__all__ = ["KEYS_VARIABLE", "cmd_models"]
