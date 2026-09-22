"""Inspect and change the person's settings only when they ask."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import any_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.settings import AUDIENCE, HttpSettingsPackClient
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError

SETTINGS_MARKDOWN = """# Settings

Read and change the person's explicit preferences, grouped by capability: music, research,
workspace, notes, Lucy herself. Never a service name.

`settings.describe` is the catalogue. `settings.get` reads one. `settings.set` writes one
they asked to change. Do not raise your own limits, shorten an erasure window, or turn on
unknown prompt-feed fields. If a setting is `never` for an assistant, explain it rather
than trying.
"""

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.settings import Setting, SettingsClient
    from lucy_api.packs.context import PackContext


class SettingsPack:
    id = "settings"
    title = "Settings"
    summary = "Explain, read and change the person's explicit assistant preferences."

    def __init__(
        self,
        base_url: str,
        *,
        audience: str = AUDIENCE,
        client: SettingsClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._override = client

    @property
    def docs(self) -> str | Path | None:
        return SETTINGS_MARKDOWN

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="settings.write",
                title="Change your settings",
                description="Change an explicit preference only after you ask for it.",
                risk="write",
                covers=("settings.set",),
            ),
        )

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: PackContext) -> Availability:
        try:
            await self._client(context).describe(profile=context.profile)
        except (NoBrokerError, ExchangeError):
            return Availability(state=State.unavailable, detail="cannot act for this person yet")
        except (DownstreamError, TransportError):
            return Availability(state=State.unavailable, detail="settings could not be reached")
        return Availability(state=State.ready, detail="preferences are available")

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        del context
        return (
            define_operation(
                {
                    "name": "settings.describe",
                    "description": (
                        "Describe available settings, their current values, types, bounds, "
                        "meaning, and whether each one is account-wide or for this "
                        "conversation's profile. Call before changing an unfamiliar setting."
                    ),
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._describe,
                }
            ),
            define_operation(
                {
                    "name": "settings.get",
                    "description": (
                        "Read one explicit preference, where the value came from, and whether "
                        "it is account-wide or for this conversation's profile."
                    ),
                    "input": object_schema({"namespace": string_schema(), "key": string_schema()}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._get,
                }
            ),
            define_operation(
                {
                    "name": "settings.set",
                    "description": (
                        "Change one preference only because the person explicitly requested it; "
                        "never tune settings for your own convenience. Account-wide settings "
                        "apply under every profile; profile-wide ones apply only to this "
                        "conversation's profile. describe reports which is which."
                    ),
                    "input": object_schema(
                        {
                            "namespace": string_schema(),
                            "key": string_schema(),
                            "value": any_schema(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._set,
                }
            ),
        )

    def _client(self, context: PackContext) -> SettingsClient:
        return self._override or HttpSettingsPackClient(
            context.http, self.base_url, audience=self.audience
        )

    async def _describe(self, run: RunContext[PackContext]) -> dict[str, Any]:
        settings = await self._client(run.ctx).describe(profile=run.ctx.profile)
        return {"settings": [_resource(item, detailed=True) for item in settings]}

    async def _get(self, run: RunContext[PackContext]) -> dict[str, Any]:
        setting = await self._client(run.ctx).get(
            str(run.input.get("namespace") or ""),
            str(run.input.get("key") or ""),
            profile=run.ctx.profile,
        )
        return _resource(setting)

    async def _set(self, run: RunContext[PackContext]) -> dict[str, Any]:
        setting = await self._client(run.ctx).set(
            str(run.input.get("namespace") or ""),
            str(run.input.get("key") or ""),
            run.input.get("value"),
            profile=run.ctx.profile,
        )
        if run.ctx.probes is not None:
            run.ctx.probes.drop(run.ctx.account_id, run.ctx.profile)
        return _resource(setting)


def _resource(setting: Setting, *, detailed: bool = False) -> dict[str, Any]:
    result = {
        "namespace": setting.namespace,
        "key": setting.key,
        "value": setting.value,
        "set": setting.chosen,
        "source": setting.source,
        "pinned": setting.pinned,
        "scope": setting.scope,
    }
    if detailed:
        result.update(
            type=setting.kind,
            summary=setting.summary,
            description=setting.description,
            bounds=setting.bounds,
        )
    return result


__all__ = ["SETTINGS_MARKDOWN", "SettingsPack"]
