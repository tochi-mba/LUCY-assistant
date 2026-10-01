"""Device sign-in for the command-line client, and approving one from another client.

A client that is not signed in asks the hub for a code and waits. The approval comes from a
client that already is -- ``lucy approve CODE`` -- which hands its own sign-in to the waiting
one. Nothing here asks for a password, and no page does either.
"""

from __future__ import annotations

import time
import webbrowser
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from lucy_api.cli.base import (
    OK,
    REFUSED,
    TIMEOUT_SECONDS,
    UNREACHABLE,
    USAGE,
    CliError,
    headers,
    unreachable,
)

if TYPE_CHECKING:
    import argparse

    from lucy_api.cli.base import Context

PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
TERMINAL_ERRORS = frozenset({"access_denied", "expired_token"})


def _object(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def sign_in(ctx: Context, url: str) -> str:
    """Open the verification page and poll until its authenticated browser decides."""
    import httpx  # noqa: PLC0415 - `lucy --help` must stay instant

    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            created = client.post(f"{url}/v1/auth/device")
            body = _object(created)
            if created.status_code != HTTPStatus.CREATED:
                message = "Lucy could not start browser sign-in"
                raise CliError(message, REFUSED, hint="check `lucy doctor`, then try again")
            required = {
                "device_code": str,
                "user_code": str,
                "verification_uri_complete": str,
                "expires_in": int,
                "interval": int,
            }
            if any(not isinstance(body.get(key), kind) for key, kind in required.items()):
                message = "Lucy returned an unreadable browser sign-in response"
                raise CliError(message, REFUSED)
            device_code = str(body["device_code"])
            user_code = str(body["user_code"])
            verification = str(body["verification_uri_complete"])
            interval = max(1, int(body["interval"]))
            deadline = time.monotonic() + max(1, int(body["expires_in"]))
            ctx.say(f"To sign in, approve code {user_code} from a Lucy client signed in as you:")
            ctx.say(f"  lucy approve {user_code}")
            ctx.say(f"({verification} says the same.) With no other client signed in, run")
            ctx.say("`lucy setup --token-stdin` with a token from your keyring instead.")
            webbrowser.open(verification, new=2)
            while time.monotonic() < deadline:
                time.sleep(interval)
                response = _poll(client, f"{url}/v1/auth/device/token", device_code)
                payload = _object(response)
                if response.status_code == HTTPStatus.OK and isinstance(
                    payload.get("access_token"), str
                ):
                    return str(payload["access_token"])
                error = payload.get("error")
                if error == PENDING:
                    continue
                if error == SLOW_DOWN:
                    interval += 5
                    continue
                if error in TERMINAL_ERRORS:
                    message = "Browser sign-in was denied or expired"
                    raise CliError(message, REFUSED, hint="run `lucy setup --force` to try again")
                message = "Lucy could not complete browser sign-in"
                raise CliError(message, REFUSED, hint="check `lucy doctor`, then try again")
    except httpx.HTTPError as exc:
        message = f"cannot reach Lucy at {url}"
        raise CliError(message, UNREACHABLE, hint=type(exc).__name__) from exc
    message = "Browser sign-in expired"
    raise CliError(message, REFUSED, hint="run `lucy setup --force` to try again")


def _poll(client: Any, endpoint: str, device_code: str) -> Any:
    """One poll, asked again once if the hub dropped the connection as it was reused.

    The hub closes a connection that has been idle for as long as the interval between
    polls, so a poll could land on one closing under it: sign-in ended as "cannot reach
    Lucy" while the hub was up and waiting. A poll changes nothing, so asking twice is safe.
    """
    import httpx  # noqa: PLC0415 - `lucy --help` must stay instant

    try:
        return client.post(endpoint, json={"device_code": device_code})
    except httpx.RemoteProtocolError:
        return client.post(endpoint, json={"device_code": device_code})


def cmd_approve(ctx: Context) -> int:
    """Approve, or refuse, another client's sign-in from this signed-in one."""
    import httpx  # noqa: PLC0415 - `lucy --help` must stay instant

    if not ctx.token:
        message = "this client is not signed in, so it has no sign-in to hand over"
        raise CliError(message, USAGE, hint="approve from a client that is signed in")
    code = str(ctx.args.code).strip().upper()
    approve = not ctx.args.deny
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{ctx.url}/v1/auth/device/authorize",
                json={"user_code": code, "approve": approve},
                headers=headers(ctx.token),
            )
    except httpx.HTTPError as exc:
        raise unreachable(ctx.url, exc) from exc
    if response.status_code == HTTPStatus.NO_CONTENT:
        said = (
            f"Approved: the client showing {code} is now signed in as you."
            if approve
            else f"Refused: the client showing {code} will not be signed in."
        )
        ctx.emit({"user_code": code, "approved": approve}, said)
        return OK
    if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        message = "the hub refused this client's own token"
        raise CliError(message, USAGE, hint="run `lucy setup --force` here first")
    body = _object(response)
    detail = body.get("error_description") or body.get("detail") or f"HTTP {response.status_code}"
    message = f"the hub did not take code {code}: {detail}"
    raise CliError(message, REFUSED)


def add_parser(sub: Any, after: argparse.ArgumentParser) -> None:
    approve = sub.add_parser(
        "approve", parents=[after], help="approve another client's sign-in from this one"
    )
    approve.add_argument("code", help="the code the waiting client shows, like ABCD-EFGH")
    approve.add_argument("--deny", action="store_true", help="refuse it instead")
    approve.set_defaults(run=cmd_approve)


__all__ = ["add_parser", "cmd_approve", "sign_in"]
