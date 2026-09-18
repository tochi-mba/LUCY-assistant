"""Browser-assisted device sign-in for the command-line client."""

from __future__ import annotations

import time
import webbrowser
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from lucy_api.cli.base import REFUSED, TIMEOUT_SECONDS, UNREACHABLE, CliError

if TYPE_CHECKING:
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
            ctx.say(f"Sign in in your browser with code {user_code}")
            ctx.say(verification)
            webbrowser.open(verification, new=2)
            while time.monotonic() < deadline:
                time.sleep(interval)
                response = client.post(
                    f"{url}/v1/auth/device/token", json={"device_code": device_code}
                )
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


__all__ = ["sign_in"]
