"""The person's settings catalogue and values, projected for the settings pack."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.transport import Sibling, field, flag, rows, text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lucy_api.packs.context import Http

SERVICE = "settings"
AUDIENCE = "settings"


@dataclass(frozen=True, slots=True)
class Setting:
    namespace: str
    key: str
    value: Any
    kind: str = ""
    summary: str = ""
    description: str = ""
    source: str = ""
    chosen: bool = False
    pinned: bool = False
    bounds: Any = None
    scope: str = ""


class SettingsClient(Protocol):
    async def describe(self, *, profile: str = "") -> tuple[Setting, ...]: ...

    async def get(self, namespace: str, key: str, *, profile: str = "") -> Setting: ...

    async def set(self, namespace: str, key: str, value: Any, *, profile: str = "") -> Setting: ...


class HttpSettingsPackClient:
    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def describe(self, *, profile: str = "") -> tuple[Setting, ...]:
        payload = await self._api.send(
            "GET", "/v1/settings/schema", params=_profile_params(profile)
        )
        return tuple(_setting(row) for row in rows(payload, "settings"))

    async def get(self, namespace: str, key: str, *, profile: str = "") -> Setting:
        path = f"/v1/settings/{_part(namespace)}/{_part(key)}"
        return _setting(await self._api.send("GET", path, params=_profile_params(profile)))

    async def set(self, namespace: str, key: str, value: Any, *, profile: str = "") -> Setting:
        path = f"/v1/settings/{_part(namespace)}/{_part(key)}"
        await self._api.send("PUT", path, body={"value": value}, params=_profile_params(profile))
        return await self.get(namespace, key, profile=profile)


class FakeSettingsPackClient:
    def __init__(self, settings: Iterable[Setting] = ()) -> None:
        self.settings = {(item.namespace, item.key): item for item in settings}
        self.writes: list[tuple[str, str, Any]] = []

    async def describe(self, *, profile: str = "") -> tuple[Setting, ...]:
        del profile
        return tuple(self.settings.values())

    async def get(self, namespace: str, key: str, *, profile: str = "") -> Setting:
        del profile
        return self.settings[(namespace, key)]

    async def set(self, namespace: str, key: str, value: Any, *, profile: str = "") -> Setting:
        del profile
        self.writes.append((namespace, key, value))
        previous = self.settings[(namespace, key)]
        updated = Setting(
            namespace=namespace,
            key=key,
            value=value,
            kind=previous.kind,
            summary=previous.summary,
            description=previous.description,
            source="account",
            chosen=True,
            pinned=previous.pinned,
            bounds=previous.bounds,
            scope=previous.scope,
        )
        self.settings[(namespace, key)] = updated
        return updated


def _setting(payload: Any) -> Setting:
    return Setting(
        namespace=text(payload, "namespace"),
        key=text(payload, "key"),
        value=field(payload, "value"),
        kind=text(payload, "type"),
        summary=text(payload, "summary"),
        description=text(payload, "description"),
        source=text(payload, "source"),
        chosen=flag(payload, "set"),
        pinned=flag(payload, "pinned"),
        bounds=field(payload, "bounds"),
        scope=text(payload, "scope"),
    )


def _profile_params(profile: str) -> dict[str, str] | None:
    return {"profile": profile} if profile else None


def _part(value: str) -> str:
    from urllib.parse import quote  # noqa: PLC0415 - keeps URL syntax at the boundary

    return quote(value, safe="")


if TYPE_CHECKING:

    def _satisfies(
        real: HttpSettingsPackClient, fake: FakeSettingsPackClient
    ) -> tuple[SettingsClient, ...]:
        return (real, fake)


__all__ = [
    "AUDIENCE",
    "FakeSettingsPackClient",
    "HttpSettingsPackClient",
    "Setting",
    "SettingsClient",
]
