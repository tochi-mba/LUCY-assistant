"""First-run configuration and diagnostics for a globally installed client."""

from __future__ import annotations

import getpass
from dataclasses import asdict
from typing import TYPE_CHECKING

from lucy_api.cli import doctor
from lucy_api.cli.base import (
    OK,
    REFUSED,
    TIMEOUT_SECONDS,
    TOKEN_VAR,
    URL_VAR,
    USAGE,
    CliError,
    fetch,
    resolve_token,
    resolve_url,
)
from lucy_api.cli.config import ConfigError, redact, save_config
from lucy_api.cli.connect import discover

if TYPE_CHECKING:
    from lucy_api.cli.base import Context


MODES = {
    "hub": "Run the hub here: lucy serve (its dependencies must already be running).",
    "family": "Run the family from its checkout: bootstrap, generate .env.family, then make up.",
    "remote": "Use the existing hub: lucy doctor, then lucy status.",
}
MAX_TOKEN_CHARS = 16_384


def _ask(ctx: Context, prompt: str, default: str) -> str:
    """Read one answer without ever blocking a pipeline on a hidden question."""
    if not ctx.interactive:
        msg = "setup needs explicit options without a terminal"
        raise CliError(msg, USAGE, hint="lucy setup --help")
    print(f"{prompt} [{default}]: ", end="", file=ctx.err, flush=True)
    assert ctx.in_ is not None  # noqa: S101 - interactive checked the stream
    answer = ctx.in_.readline()
    if not answer:
        msg = "setup cancelled; no configuration was changed"
        raise CliError(msg, REFUSED)
    return answer.strip() or default


def _mode(ctx: Context) -> str:
    selected = ctx.args.mode or ctx.config.get("mode")
    if not selected:
        if ctx.args.yes or ctx.args.dry_run:
            selected = "hub"
        else:
            ctx.say(
                "Choose hub (this machine), family (all services), or remote (an existing hub)."
            )
            selected = _ask(ctx, "How should Lucy run?", "hub")
    if selected not in MODES:
        msg = "choose a mode: hub, family, or remote"
        raise CliError(msg, USAGE, hint="lucy setup --mode hub")
    return str(selected)


def _setup_url(ctx: Context, mode: str) -> str:
    if ctx.interactive and not (ctx.args.yes or ctx.args.dry_run or ctx.args.url):
        return resolve_url(_ask(ctx, "Hub URL", ctx.url), {})
    if mode == "remote" and not (ctx.args.url or ctx.environ.get(URL_VAR) or ctx.config.get("url")):
        msg = "remote setup needs the hub's address"
        raise CliError(msg, USAGE, hint="pass --url https://your-hub")
    return ctx.url


def _setup_token(ctx: Context, url: str) -> str:
    if ctx.args.no_token:
        return ""
    existing = ctx.config.get("token") if url == ctx.config.get("url").rstrip("/") else ""
    if ctx.args.dry_run:
        return existing
    if ctx.args.token_stdin:
        if ctx.in_ is None:
            msg = "no token input is available"
            raise CliError(msg, USAGE)
        token = ctx.in_.read(MAX_TOKEN_CHARS + 1).strip()
        if not token or len(token) > MAX_TOKEN_CHARS:
            msg = "provide one non-empty token of at most 16384 characters on stdin"
            raise CliError(msg, USAGE)
        return resolve_token({TOKEN_VAR: token})
    if ctx.interactive and not ctx.args.yes and not ctx.environ.get(TOKEN_VAR):
        ctx.say(
            "Browser sign-in is not available in this hub yet. A token must have audience lucy-api."
        )
        ctx.say("Leave this blank to keep the saved token, or continue without signing in.")
        token = getpass.getpass("Keyring token (hidden): ", stream=ctx.err).strip()
        return resolve_token({TOKEN_VAR: token or existing})
    return existing


def cmd_setup(ctx: Context) -> int:
    """Save the choices once, with a preview and explicit overwrite semantics."""
    mode = _mode(ctx)
    url = _setup_url(ctx, mode)
    if ctx.config.exists and not (ctx.args.force or ctx.args.dry_run):
        if not ctx.interactive or ctx.args.yes:
            msg = "a configuration already exists"
            raise CliError(
                msg,
                USAGE,
                hint="inspect lucy config; use --force to replace it",
            )
        if _ask(ctx, "Replace the saved configuration? yes/no", "no").lower() != "yes":
            msg = "setup cancelled; no configuration was changed"
            raise CliError(msg, REFUSED)
    token = _setup_token(ctx, url)
    values = {"url": url, "mode": mode, "token": token}
    if not ctx.args.dry_run:
        try:
            save_config(values, ctx.environ)
        except ConfigError as exc:
            raise CliError(str(exc), USAGE) from exc
    notices = [
        f"{variable} in this shell overrides the saved configuration."
        for variable in (URL_VAR, TOKEN_VAR)
        if ctx.environ.get(variable, "").strip()
    ]
    if not token:
        notices.append(
            "No token saved. Account features need sign-in; service readiness can still be checked."
        )
    services: object = None
    result = OK
    if ctx.args.capabilities and not ctx.args.dry_run:
        try:
            services = discover(url, resolve_token(ctx.environ) or token)
        except CliError as exc:
            notices.append(f"Configuration saved; capability discovery needs attention: {exc}")
            result = exc.code
    payload = {
        "path": str(ctx.config.path),
        "saved": not ctx.args.dry_run,
        "mode": mode,
        "url": url,
        "token_saved": bool(token),
        "next": MODES[mode],
        "notices": notices,
        "services": services,
    }
    verb = "Would save" if ctx.args.dry_run else "Saved"
    lines = [f"{verb} configuration at {ctx.config.path}", f"Hub: {url}", MODES[mode], *notices]
    if services is not None:
        lines.append(
            "Run lucy connect to review capability setup, or lucy connect music for music."
        )
    ctx.emit(payload, "\n".join(lines))
    return result


def cmd_config(ctx: Context) -> int:
    """Show effective configuration and its source, with the credential redacted."""
    url_source = (
        "flag"
        if ctx.args.url
        else "environment"
        if ctx.environ.get(URL_VAR)
        else "file"
        if ctx.config.get("url")
        else "default"
    )
    token_source = (
        "environment" if ctx.environ.get(TOKEN_VAR, "").strip() else "file" if ctx.token else "none"
    )
    payload = {
        "path": str(ctx.config.path),
        "exists": ctx.config.exists,
        "url": {"value": ctx.url, "source": url_source},
        "token": {"value": redact(ctx.token), "source": token_source},
        "mode": ctx.config.get("mode") or "unset",
        "unknown_keys": list(ctx.config.unknown),
    }
    ctx.emit(
        payload,
        "\n".join(
            [
                f"Config: {ctx.config.path}",
                f"Hub: {ctx.url} ({url_source})",
                f"Token: {redact(ctx.token) or 'not saved'} ({token_source})",
                f"Mode: {payload['mode']}",
                f"Unknown keys: {', '.join(ctx.config.unknown) or 'none'}",
            ]
        ),
    )
    return OK


def cmd_doctor(ctx: Context) -> int:
    """Check the local installation and the hub without changing either."""
    import httpx  # noqa: PLC0415 - help does not need a network stack

    checks = doctor.environment_checks(config=ctx.config, environ=ctx.environ, token=ctx.token)
    responses = None
    error = None
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            ready = fetch(client, ctx.url, "/ready", "")
            me = fetch(client, ctx.url, "/v1/me", ctx.token) if ctx.token else None
        try:
            body = ready.json()
            raw_checks = body.get("checks", {}) if isinstance(body, dict) else {}
            states = (
                {
                    name: check.get("status", "unknown") if isinstance(check, dict) else "unknown"
                    for name, check in raw_checks.items()
                }
                if isinstance(raw_checks, dict)
                else {}
            )
        except ValueError:
            states = {}
        responses = {"ready": ready, "me": me, "checks": states}
    except CliError as exc:
        error = str(exc)
    checks.extend(doctor.hub_checks(ctx.url, responses, error))
    status = doctor.worst(checks)
    text = "\n".join(
        f"{check.level:4} {check.name}: {check.detail}"
        + (f"\n     {check.fix}" if check.fix else "")
        for check in checks
    )
    ctx.emit({"status": status, "checks": [asdict(check) for check in checks]}, text)
    return REFUSED if status == doctor.FAIL else OK
