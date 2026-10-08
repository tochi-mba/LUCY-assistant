"""Inspect and change the person's settings only when they ask."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import any_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.settings import AUDIENCE, HttpSettingsPackClient
from lucy_api.context.types import Trust
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.prompt.docs import capability_doc
from lucy_api.settings.catalogue import AgentAccess, knob
from lucy_api.settings.groups import group_for

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.settings import Setting, SettingsClient
    from lucy_api.packs.context import PackContext


class SettingsRefusedError(ValueError):
    """A write the model asked for that only the person may make."""


ADDRESSED = "As settings.describe lists it."
"""Where a namespace and a key come from: describe's rows, service names and all."""


NEVER_SENTENCE = (
    "{namespace}.{key} can only be changed by the person, in their settings, and never by an "
    "assistant, even with approval. Tell them where to change it."
)

MAY_WRITE = frozenset({AgentAccess.WITH_APPROVAL.value, AgentAccess.FREELY.value})
"""What a setting must declare before a model may write it. Anything else -- `never`, or a
service that did not say -- is the person's to change: settings-api's own rule is that a
setting nobody thought about is one an assistant may not touch."""


def _own(namespace: str, key: str) -> str:
    """Lucy's own catalogue's answer for its own namespace, or empty for anyone else's."""
    item = knob(key) if namespace == "lucy" else None
    return item.agent.value if item is not None else ""


def _access(setting: Setting) -> str:
    """Whether an assistant may change this, as the hub knows it.

    Its own namespace from its own catalogue, so a stale settings-api cannot loosen it; every
    other namespace as settings-api declares it. Only `lucy.*` was ever answered, so every
    sibling setting reached the model with no word on whether it could be changed at all.
    """
    return _own(setting.namespace, setting.key) or setting.agent


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
        self._declared: dict[tuple[str, str], str] = {}
        """What settings-api said each setting allows an assistant, from every row read.
        The gate decides before anything runs and cannot ask the network; a setting it has
        not seen is one it asks about."""

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="settings.write",
                title="Change your settings",
                description="Change an explicit preference only after you ask for it.",
                risk="write",
                covers=("settings.set",),
                each_call=self._needs_a_yes,
                refuses=self._never,
            ),
        )

    def _never(self, arguments: Mapping[str, object]) -> str:
        """The sentence for a setting no assistant may change, or nothing.

        Decided from the hub's own catalogue and from what settings-api declared on rows
        already read; a setting never seen is not refused here, it is asked about, and the
        write itself still checks.
        """
        namespace = str(arguments.get("namespace") or "")
        key = str(arguments.get("key") or "")
        access = _own(namespace, key) or self._declared.get((namespace, key), "")
        if access != AgentAccess.NEVER.value:
            return ""
        return NEVER_SENTENCE.format(namespace=namespace, key=key)

    def _needs_a_yes(self, arguments: Mapping[str, object]) -> bool:
        """Whether changing this setting needs the person's yes to that change alone.

        Only `freely` goes without, and `never` is refused when it runs, so asking would be
        a question whose yes changes nothing. Everything else -- `with_approval`, or a
        setting not seen yet -- is asked about in every mode: `auto` used to change a
        `with_approval` setting without a word.
        """
        namespace = str(arguments.get("namespace") or "")
        key = str(arguments.get("key") or "")
        access = _own(namespace, key) or self._declared.get((namespace, key), "")
        return access not in {AgentAccess.FREELY.value, AgentAccess.NEVER.value}

    def _remember(self, settings: Sequence[Setting]) -> None:
        for item in settings:
            if item.agent:
                self._declared[(item.namespace, item.key)] = item.agent

    def result_trust(self, operation: str, data: object) -> Trust:
        """The person's own settings, read from their store."""
        del operation, data
        return Trust.observed

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
                        "List the person's settings for one capability, each with its value, "
                        "bounds, scope and whether you may change it. Omit `capability` for "
                        "every one, which is long. settings.get gives one setting in full."
                    ),
                    "input": object_schema(
                        {
                            "capability": string_schema()
                            .optional()
                            .describe(
                                "lucy, account, persona, memory, music, research, workspace "
                                "or repos."
                            )
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._describe,
                }
            ),
            define_operation(
                {
                    "name": "settings.get",
                    "description": (
                        "Read one setting in full: what it means, its value, where the value "
                        "came from, and whether it is account-wide or for this profile."
                    ),
                    "input": object_schema(
                        {
                            "namespace": string_schema().describe(ADDRESSED),
                            "key": string_schema().describe(ADDRESSED),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._get,
                }
            ),
            define_operation(
                {
                    "name": "settings.set",
                    "description": (
                        "Change one setting the person asked to change -- never for your own "
                        "convenience. `scope` from settings.describe says whether it applies "
                        "to every profile or only this one."
                    ),
                    "input": object_schema(
                        {
                            "namespace": string_schema().describe(ADDRESSED),
                            "key": string_schema().describe(ADDRESSED),
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
        self._remember(settings)
        wanted = str(run.input.get("capability") or "").strip().lower()
        listed = [_resource(item, listing=True) for item in settings]
        if wanted:
            listed = [item for item in listed if item["capability"] == wanted]
        return {"settings": listed}

    async def _get(self, run: RunContext[PackContext]) -> dict[str, Any]:
        setting = await self._client(run.ctx).get(
            str(run.input.get("namespace") or ""),
            str(run.input.get("key") or ""),
            profile=run.ctx.profile,
        )
        self._remember((setting,))
        return _resource(setting, full=True)

    async def _set(self, run: RunContext[PackContext]) -> dict[str, Any]:
        namespace = str(run.input.get("namespace") or "")
        key = str(run.input.get("key") or "")
        # Settings-api cannot tell a model's write from the person's and says the hub must
        # apply its declaration. Only `lucy.*` was checked, so a model could switch off a
        # protection in any other namespace -- every setting in user, keyring and memory is
        # `never` -- in `auto` without a word, or after one "yes" in `ask`.
        access = _own(namespace, key)
        if not access:
            client = self._client(run.ctx)
            access = (await client.get(namespace, key, profile=run.ctx.profile)).agent
        if access not in MAY_WRITE:
            raise SettingsRefusedError(NEVER_SENTENCE.format(namespace=namespace, key=key))
        setting = await self._client(run.ctx).set(
            namespace,
            key,
            run.input.get("value"),
            profile=run.ctx.profile,
        )
        if run.ctx.probes is not None:
            run.ctx.probes.drop(run.ctx.account_id, run.ctx.profile)
        if run.ctx.forget_settings is not None:
            run.ctx.forget_settings(namespace)
        return _resource(setting)


def _resource(setting: Setting, *, listing: bool = False, full: bool = False) -> dict[str, Any]:
    """One setting as the model sees it: addressed by namespace and key, placed by capability.

    The address is settings-api's, because that is what `settings.get` and `settings.set`
    take. The placement is Lucy's: `spotify.default_market` is a Music setting, and
    `lucy.feeds_music_now_playing` is one too, whichever namespace stores it.

    A listing carries the one-line summary and leaves the long description to `settings.get`:
    describe returned both for every setting, and the lucy namespace alone came to eleven
    thousand characters of it, read again on every later round of the turn.
    """
    result = {
        "capability": group_for(f"{setting.namespace}.{setting.key}"),
        "namespace": setting.namespace,
        "key": setting.key,
        "value": setting.value,
        "set": setting.chosen,
        "source": setting.source,
        "pinned": setting.pinned,
        "scope": setting.scope,
    }
    if listing or full:
        result.update(type=setting.kind, summary=setting.summary, bounds=setting.bounds)
        access = _access(setting)
        if access:
            result["assistant"] = access
    if full:
        result["description"] = setting.description
    return result


__all__ = ["SettingsPack"]
