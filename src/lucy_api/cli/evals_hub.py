"""The eval harness's :class:`~lucy_api.evals.hub.Hub`, over HTTP.

Not a second client: it is handed the ``httpx.Client`` the command opened, the URL and token
:class:`~lucy_api.cli.base.Context` resolved -- flag, environment, config file, in that
order -- and it authenticates with :func:`~lucy_api.cli.base.headers`, exactly as
``lucy talk`` and ``lucy models`` do. What it adds is the route list the harness needs and
one translation: an answer outside 2xx becomes a :class:`~lucy_api.evals.hub.HubError`
carrying the hub's own problem ``detail``, and no answer at all becomes
:class:`~lucy_api.evals.hub.HubUnreachable`.

Every write that the hub makes idempotent gets a fresh ``Idempotency-Key``, so a retried
request can never start a second session or say a message twice.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import time
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from lucy_api.cli.base import headers
from lucy_api.cli.setup import MAX_TOKEN_CHARS
from lucy_api.evals.hub import HubError, HubUnreachable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

PAGE = 100
"""The largest page the hub serves; a transcript is read in as few requests as it allows."""

ERROR_FROM = 400

TURN_HEADROOM_SECONDS = 600.0
"""How long a token must still have to run when a turn is started on it.

The hub acts for the person on their own token for as long as a turn runs. A turn started on
one with two minutes left failed part way through: every call to a sibling after the expiry
was refused, and the scenario was marked failed for a reason that was the harness's. Ten
minutes is longer than any turn a scenario holds.
"""


class HttpHub:
    """The routes a conversation needs, on one authenticated client."""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        token: str,
        *,
        renew: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``renew`` is asked for a fresh token when the hub refuses the one in use: a keyring
        token lives fifteen minutes, and a conversation with helpers and restarts outlives it.
        It is asked once per refusal; refused again, the run stops as it always did. It is
        also asked before a turn starts on a token too close to its end to see the turn out.
        """
        self._client = client
        self._url = url.rstrip("/")
        self._token = token
        self._renew = renew
        self._clock = clock

    def health(self) -> dict[str, Any]:
        return self._object("GET", "/healthy")

    def models(self) -> dict[str, Any]:
        return self._object("GET", "/v1/models")

    def prompt(self, profile: str) -> dict[str, Any]:
        """The fixed prompt every request carries, priced section by section."""
        return self._object("GET", "/v1/prompt/preview", params={"profile": profile})

    def capabilities(self, profile: str) -> list[dict[str, Any]]:
        return self._rows("GET", "/v1/capabilities", params={"profile": profile})

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._object("POST", "/v1/sessions", body=body, idempotent=True)

    def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        event = {"type": "input.message", "content": text}
        return self._inputs(session_id, event)

    def answer_approval(
        self, session_id: str, approval_id: str, *, approved: bool, lifetime: str
    ) -> dict[str, Any]:
        event = {
            "type": "input.approval",
            "approval_id": approval_id,
            "approved": approved,
            "lifetime": lifetime,
        }
        return self._inputs(session_id, event)

    def turn(self, turn_id: str) -> dict[str, Any]:
        return self._object("GET", f"/v1/turns/{_segment(turn_id)}")

    def cancel_turn(self, turn_id: str) -> dict[str, Any]:
        return self._object("POST", f"/v1/turns/{_segment(turn_id)}/cancel")

    def items(self, session_id: str, after: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": PAGE, "order": "asc"}
        if after:
            params["after"] = after
        return self._object("GET", f"/v1/sessions/{_segment(session_id)}/items", params=params)

    def usage(self, session_id: str) -> dict[str, Any]:
        return self._object("GET", f"/v1/sessions/{_segment(session_id)}/usage")

    def tools(self, session_id: str) -> dict[str, Any]:
        return self._object("GET", "/v1/tools", params={"session_id": session_id})

    def invoke(self, operation: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any]:
        body = {"input": arguments, "session_id": session_id}
        return self._object("POST", f"/v1/tools/{_segment(operation)}/invoke", body=body)

    def permissions(self, profile: str) -> list[dict[str, Any]]:
        return self._rows("GET", "/v1/permissions", params={"profile": profile})

    def grant(self, permission: str, profile: str) -> None:
        body = {"permission": permission, "decision": "allow", "profile": profile}
        self._send("PUT", "/v1/permissions", body=body)

    def revoke(self, permission: str, profile: str) -> None:
        path = f"/v1/permissions/{_segment(permission)}"
        self._send("DELETE", path, params={"profile": profile})

    def archive(self, session_id: str) -> None:
        self._send("PATCH", f"/v1/sessions/{_segment(session_id)}", body={"archived": True})

    def _inputs(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        self._fresh_for_a_turn()
        path = f"/v1/sessions/{_segment(session_id)}/inputs"
        return self._object("POST", path, body={"events": [event]}, idempotent=True)

    def _fresh_for_a_turn(self) -> None:
        """Renew before a turn, not after it fails: the hub accepted the old token at the start."""
        if self._renew is None:
            return
        left = _seconds_left(self._token, self._clock())
        if left is not None and left < TURN_HEADROOM_SECONDS:
            self._token = self._renew()

    def _rows(self, method: str, path: str, *, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        data = self._object(method, path, params=params).get("data")
        if not isinstance(data, list):
            message = f"{method} {path} answered without a data list"
            raise HubError(message)
        return [row for row in data if isinstance(row, dict)]

    def _object(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        response = self._send(method, path, body=body, params=params, idempotent=idempotent)
        try:
            parsed = response.json()
        except ValueError as exc:
            message = f"{method} {path} answered with something that is not JSON"
            raise HubError(message, status=response.status_code) from exc
        if not isinstance(parsed, dict):
            message = f"{method} {path} answered with JSON that is not an object"
            raise HubError(message, status=response.status_code)
        return parsed

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        idempotent: bool = False,
    ) -> httpx.Response:
        response = self._request(method, path, body=body, params=params, idempotent=idempotent)
        if response.status_code == HTTPStatus.UNAUTHORIZED and self._renew is not None:
            # Refused before anything was done, so sending it again under a fresh token
            # cannot do it twice. Refused again, it stops the run as before.
            self._token = self._renew()
            response = self._request(method, path, body=body, params=params, idempotent=idempotent)
        if response.status_code >= ERROR_FROM:
            answered = _body(response)
            raise HubError(
                _problem(method, path, response, answered),
                status=response.status_code,
                problem=_kind(answered),
            )
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None,
        params: Mapping[str, Any] | None,
        idempotent: bool,
    ) -> httpx.Response:
        sent = headers(self._token)
        if idempotent:
            sent["Idempotency-Key"] = str(uuid.uuid4())
        try:
            return self._client.request(
                method, f"{self._url}{path}", headers=sent, json=body, params=params
            )
        except httpx.HTTPError as exc:
            message = f"cannot reach Lucy at {self._url}"
            raise HubUnreachable(message) from exc


TOKEN_COMMAND_SECONDS = 60.0
"""How long ``--token-command`` may take to print a token."""


def renewed_token(command: str) -> str:
    """A fresh token from the operator's own ``--token-command``, held in memory only.

    Whatever the command prints is the token, so nothing here ever repeats its output: a
    command that fails is named by its exit status alone. A failure is a refusal, which
    stops the run the way an expired token always has.
    """
    # Into a file, not a pipe: at the timeout only the shell is killed, and a program it
    # started would hold a pipe open -- and this call with it -- for as long as it ran.
    with tempfile.TemporaryFile() as sink:
        try:
            ended = subprocess.run(  # noqa: S602 - the operator's own command, named on the command line
                command,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.DEVNULL,
                timeout=TOKEN_COMMAND_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            message = f"the token command did not finish in {TOKEN_COMMAND_SECONDS:.0f}s"
            raise HubError(message, status=HTTPStatus.UNAUTHORIZED) from exc
        sink.seek(0)
        token = sink.read(MAX_TOKEN_CHARS + 1).decode("utf-8", errors="replace").strip()
    if (
        ended.returncode != 0
        or not token
        or len(token) > MAX_TOKEN_CHARS
        or any(character.isspace() for character in token)
    ):
        message = f"the token command exited {ended.returncode} without printing one token"
        raise HubError(message, status=HTTPStatus.UNAUTHORIZED)
    return token


def _seconds_left(token: str, now: float) -> float | None:
    """How long a JWT says it has, read unverified and only to decide when to ask for another.

    Nothing is trusted on the strength of it: the hub verifies every token it is sent. A token
    that is not a JWT, or carries no expiry, says nothing, and is renewed only when refused.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"]) - now
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _problem(method: str, path: str, response: httpx.Response, body: dict[str, Any]) -> str:
    """The hub's own sentence for what went wrong, which its errors are written to carry."""
    detail = body.get("detail")
    reason = detail if isinstance(detail, str) and detail else response.reason_phrase or "no detail"
    return f"{method} {path} answered {response.status_code}: {reason}"


def _kind(body: dict[str, Any]) -> str:
    """The last part of a problem ``type`` URI: ``.../settings-unavailable`` names the problem."""
    kind = body.get("type")
    return kind.rstrip("/").rsplit("/", 1)[-1] if isinstance(kind, str) else ""


def _segment(value: str) -> str:
    """An id placed in a path, encoded so it cannot climb out of the segment it is in."""
    return quote(value, safe="")


__all__ = ["HttpHub", "renewed_token"]
