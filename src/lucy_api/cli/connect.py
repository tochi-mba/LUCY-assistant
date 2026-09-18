"""Read service setup guidance from the hub that owns the deployment."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from lucy_api.cli.base import OK, REFUSED, TIMEOUT_SECONDS, USAGE, CliError, fetch

if TYPE_CHECKING:
    from lucy_api.cli.base import Context


def discover(url: str, token: str) -> list[dict[str, Any]]:
    """Fetch the authenticated catalogue; never substitute a local guessed list."""
    import httpx  # noqa: PLC0415 - help does not need a network stack

    if not token:
        msg = "capability setup needs a Lucy token"
        raise CliError(msg, REFUSED, hint="run lucy setup to save one")
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        response = fetch(client, url, "/v1/setup", token)
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        msg = "the hub refused the token"
        raise CliError(
            msg,
            REFUSED,
            hint="run lucy setup --force with a current lucy-api token",
        )
    if response.status_code == HTTPStatus.NOT_FOUND:
        msg = "this hub does not offer setup discovery yet"
        raise CliError(
            msg,
            REFUSED,
            hint="update the hub, then run lucy connect",
        )
    if response.status_code != HTTPStatus.OK:
        msg = "the hub cannot report setup requirements right now"
        raise CliError(msg, REFUSED, hint="run lucy doctor")
    try:
        body = response.json()
        services = _validate_services(body["services"])
    except (ValueError, KeyError, TypeError) as exc:
        msg = "the hub returned an unreadable setup catalogue"
        raise CliError(msg, REFUSED, hint="update the hub and client together") from exc
    return services


def _validate_services(services: Any) -> list[dict[str, Any]]:
    """Refuse malformed data before rendering it as a successful setup answer."""
    if not isinstance(services, list) or any(
        not isinstance(row, dict)
        or not all(
            isinstance(row.get(key), str)
            for key in ("id", "title", "state", "connection_state", "summary")
        )
        or not isinstance(row.get("actions"), list)
        or any(
            not isinstance(action, dict) or not isinstance(action.get("description"), str)
            for action in row["actions"]
        )
        for row in services
    ):
        raise ValueError
    return services


def cmd_connect(ctx: Context) -> int:
    """List capabilities or explain the selected capability's actual setup steps."""
    services = discover(ctx.url, ctx.token)
    if ctx.args.capability is None:
        lines = [
            f"{row['id']:<12} {row['state']}; account connection: {row['connection_state']}"
            for row in services
        ]
        ctx.emit(
            {"services": services},
            "\n".join([*lines, "Run lucy connect <capability> for setup instructions."]),
        )
        return OK
    selected = next((row for row in services if row["id"] == ctx.args.capability), None)
    if selected is None:
        msg = "that capability is not in this hub's catalogue"
        raise CliError(
            msg,
            USAGE,
            hint="run lucy connect to list the available names",
        )
    lines = [
        selected["title"],
        selected["summary"],
        *[
            action["description"] + (f" {action['url']}" if action.get("url") else "")
            for action in selected["actions"]
        ],
    ]
    complete = selected["state"] == "ready" and selected["connection_state"] in {
        "not_required",
        "connected",
    }
    if not complete:
        lines.append(
            "This capability still needs a connected account. Follow the steps above; "
            "Lucy starts the browser flow and never asks for a password."
        )
    ctx.emit({"service": selected, "changed": False, "complete": complete}, "\n".join(lines))
    return OK if complete else REFUSED
