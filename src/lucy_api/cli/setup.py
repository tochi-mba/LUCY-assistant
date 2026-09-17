"""First-run configuration and diagnostics for a globally installed client."""

from __future__ import annotations

import getpass
from dataclasses import asdict
from pathlib import Path
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
from lucy_api.cli.config import ConfigError, load_config, redact, save_config
from lucy_api.cli.connect import discover
from lucy_api.cli.family import (
    APP_PAGE,
    PACKAGE_ROOT,
    checkout_hint,
    extra_desk,
    find_family_root,
    github_app_state,
    github_ci_notice,
    run_github_ci,
    should_install_github_app,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


def _wants_github_ci(ctx: Context, mode: str) -> bool:
    return should_install_github_app(ctx, mode)


def _intends_to_change(ctx: Context) -> bool:
    """Flags mean the person is updating setup; a bare rerun should keep what they have."""
    if ctx.args.force:
        return True
    if not ctx.config.exists:
        return True
    return bool(ctx.args.mode or ctx.args.url or ctx.args.token_stdin or ctx.args.no_token)


def _status_rows(
    ctx: Context, mode: str, url: str, token: str, root: Path | None
) -> list[dict[str, str | bool]]:
    rows: list[dict[str, str | bool]] = [
        {
            "id": "config",
            "done": ctx.config.exists,
            "label": "Config",
            "detail": str(ctx.config.path) if ctx.config.exists else "not saved yet",
        },
        {"id": "hub", "done": True, "label": "Hub", "detail": f"{url} ({mode})"},
        {
            "id": "token",
            "done": bool(token),
            "label": "Token",
            "detail": "saved" if token else "not saved",
        },
    ]
    if root is None:
        rows.append(
            {"id": "family", "done": False, "label": "Family checkout", "detail": "not found"}
        )
        rows.append(
            {
                "id": "github_app",
                "done": False,
                "label": "GitHub App",
                "detail": f"needs a family checkout to install {APP_PAGE}",
            }
        )
        return rows
    rows.append({"id": "family", "done": True, "label": "Family checkout", "detail": str(root)})
    app = github_app_state(root)
    rows.append(
        {
            "id": "github_app",
            "done": bool(app["done"]),
            "label": "GitHub App",
            "detail": str(app["detail"]),
        }
    )
    extras = extra_desk(root)
    present = sum(1 for row in extras["checkouts"] if row["present"])
    parts = []
    if extras["manifest"]:
        parts.append(f"{present}/{len(extras['checkouts'])} local checkouts")
    if extras["compose_override"]:
        parts.append("compose override")
    if extras["genenv"]:
        parts.append("token extras")
    rows.append(
        {
            "id": "extras",
            "done": bool(parts),
            "label": "Private extras",
            "detail": ", ".join(parts) if parts else "none",
        }
    )
    return rows


def _render_status(ctx: Context, rows: list[dict[str, str | bool]]) -> list[str]:
    done = ctx.style.good("done")
    nxt = ctx.style.warn("next")
    width = max(len(str(row["label"])) for row in rows)
    lines = ["Setup"]
    for row in rows:
        mark = done if row["done"] else nxt
        lines.append(f"  {mark}  {str(row['label']).ljust(width)}  {row['detail']}")
    return lines


def _resolve_choices(ctx: Context, mode: str, *, changing: bool) -> tuple[str, str, bool, bool]:
    """Where the hub is and who we are: asked for and written down, or read back.

    A dry run resolves everything and writes nothing, which is what makes it worth running:
    a person can see exactly what would be saved before any of it is.
    """
    if not changing:
        token = ctx.token if not ctx.args.dry_run else ctx.config.get("token")
        return ctx.url, token, False, ctx.config.exists

    url = _setup_url(ctx, mode)
    token = _setup_token(ctx, url)
    if ctx.args.dry_run:
        return url, token, False, False
    try:
        save_config({"url": url, "mode": mode, "token": token}, ctx.environ)
    except ConfigError as exc:
        raise CliError(str(exc), USAGE) from exc
    ctx.config = load_config(ctx.environ)
    return url, token, True, False


def _connect_github_ci(
    ctx: Context,
    mode: str,
    rows: Sequence[Mapping[str, object]],
    root: Path | None,
    notices: list[str],
) -> tuple[dict[str, object] | None, int, bool]:
    """Install the family CI app, or explain why that is not possible here.

    Returns what happened, the exit code it earned, and whether the status rows are now
    stale -- a successful install changes what the next status read would say, and showing
    the pre-install answer would tell somebody their setup did not work when it did.
    """
    if not _wants_github_ci(ctx, mode):
        return None, OK, False
    if root is None:
        notices.append(checkout_hint())
        return {"ok": False, "checkout": None, "already": False}, REFUSED, False

    already = any(row["id"] == "github_app" and row["done"] for row in rows)
    if already and not ctx.args.github_ci:
        notices.append(github_ci_notice(0, dry_run=bool(ctx.args.dry_run), already=True))
        return {"ok": True, "checkout": str(root), "already": True}, OK, False

    try:
        code = run_github_ci(ctx, root)
    except CliError as exc:
        notices.append(str(exc))
        code = exc.code
    else:
        notices.append(github_ci_notice(code, dry_run=bool(ctx.args.dry_run)))
    return {"ok": code == 0, "checkout": str(root), "already": False}, code, code == 0


def cmd_setup(ctx: Context) -> int:
    """Save the choices once, show what is already in place, and finish what is not."""
    mode = _mode(ctx)
    changing = _intends_to_change(ctx)
    already_set_up = ctx.config.exists and not changing and not ctx.args.dry_run
    if already_set_up and ctx.interactive and not ctx.args.yes:
        ctx.say("Lucy is already set up on this machine. Existing values are listed next.")
        if _ask(ctx, "Change the saved hub settings? yes/no", "no").lower() == "yes":
            changing = True
    url, token, saved, keep_existing = _resolve_choices(ctx, mode, changing=changing)

    notices = [
        f"{variable} in this shell overrides the saved configuration."
        for variable in (URL_VAR, TOKEN_VAR)
        if ctx.environ.get(variable, "").strip()
    ]
    if not token:
        notices.append(
            "No token saved. Account features need sign-in; service readiness can still be checked."
        )
    root = find_family_root(cwd=Path.cwd(), environ=ctx.environ, origin=PACKAGE_ROOT)
    rows = _status_rows(ctx, mode, url, token, root)
    github_ci, result_github, refreshed = _connect_github_ci(ctx, mode, rows, root, notices)
    if refreshed:
        rows = _status_rows(ctx, mode, url, token, root)
    services: object = None
    result = OK
    if ctx.args.capabilities and not ctx.args.dry_run:
        try:
            services = discover(url, resolve_token(ctx.environ) or token)
        except CliError as exc:
            notices.append(f"Configuration saved; capability discovery needs attention: {exc}")
            result = exc.code
    if result_github != OK and result == OK:
        result = result_github
    payload = {
        "path": str(ctx.config.path),
        "saved": saved,
        "kept": keep_existing,
        "mode": mode,
        "url": url,
        "token_saved": bool(token),
        "next": MODES[mode],
        "notices": notices,
        "services": services,
        "github_ci": github_ci,
        "already": rows,
    }
    verb = "Would save" if ctx.args.dry_run else "Saved" if saved else "Using"
    lines = [
        *_render_status(ctx, rows),
        "",
        f"{verb} configuration at {ctx.config.path}",
        f"Hub: {url}",
        MODES[mode],
        *notices,
    ]
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
