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

import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from lucy_api.cli.base import headers
from lucy_api.evals.hub import HubError, HubUnreachable

if TYPE_CHECKING:
    from collections.abc import Mapping

PAGE = 100
"""The largest page the hub serves; a transcript is read in as few requests as it allows."""

ERROR_FROM = 400


class HttpHub:
    """The routes a conversation needs, on one authenticated client."""

    def __init__(self, client: httpx.Client, url: str, token: str) -> None:
        self._client = client
        self._url = url.rstrip("/")
        self._token = token

    def health(self) -> dict[str, Any]:
        return self._object("GET", "/healthy")

    def models(self) -> dict[str, Any]:
        return self._object("GET", "/v1/models")

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
        path = f"/v1/sessions/{_segment(session_id)}/inputs"
        return self._object("POST", path, body={"events": [event]}, idempotent=True)

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
        sent = headers(self._token)
        if idempotent:
            sent["Idempotency-Key"] = str(uuid.uuid4())
        try:
            response = self._client.request(
                method, f"{self._url}{path}", headers=sent, json=body, params=params
            )
        except httpx.HTTPError as exc:
            message = f"cannot reach Lucy at {self._url}"
            raise HubUnreachable(message) from exc
        if response.status_code >= ERROR_FROM:
            raise HubError(_problem(method, path, response), status=response.status_code)
        return response


def _problem(method: str, path: str, response: httpx.Response) -> str:
    """The hub's own sentence for what went wrong, which its errors are written to carry."""
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    reason = detail if isinstance(detail, str) and detail else response.reason_phrase or "no detail"
    return f"{method} {path} answered {response.status_code}: {reason}"


def _segment(value: str) -> str:
    """An id placed in a path, encoded so it cannot climb out of the segment it is in."""
    return quote(value, safe="")


__all__ = ["HttpHub"]
