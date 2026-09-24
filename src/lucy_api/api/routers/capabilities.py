"""What the model can do, and what it can actually see this turn.

Two reads, on purpose. ``GET /v1/capabilities`` is the catalogue joined with connection
state: every pack this deployment installed, whether it is usable for this person, and a
sentence saying why not if it is not. ``GET /v1/tools`` is the registry the model would
be bound with right now, after deferred loading. A client that only has the first cannot
answer "why did it not call music.play"; a client that only has the second cannot offer
a connect button.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, StoreDep
from lucy_api.api.schemas.problem import Problem
from lucy_api.core.container import PackRequest
from lucy_api.packs.context import PackContext
from lucy_api.permissions.store import grants_for
from lucy_api.sessions.scope import WorkspaceScope

router = APIRouter(prefix="/v1", tags=["capabilities"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_AUTHED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
}
_INVOKE: dict[int | str, dict[str, Any]] = {
    **_AUTHED,
    status.HTTP_409_CONFLICT: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}


class InvokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)
    session_id: str = Field(default="", max_length=64)
    profile: str = Field(default="personal", min_length=1, max_length=128)


@router.get(
    "/capabilities",
    operation_id="list_capabilities",
    summary="Every capability, its state, and one line each",
    responses=_AUTHED,
    description=(
        "The catalogue a person sees. An unconnected capability is still listed here so "
        "the UI can offer to connect it; the model's tool list hides it until it is ready."
    ),
)
async def list_capabilities(
    acting: ActingAsDep,
    container: ContainerDep,
    profile: Annotated[str, Query(min_length=1, max_length=128)] = "personal",
) -> dict[str, Any]:
    """Probe every installed pack for this person and return the listings."""
    pack_ctx = container.pack_context(
        PackRequest(caller=acting.caller, user_token=acting.token, profile=profile, session_id="")
    )
    catalogue = await container.capabilities.probe(pack_ctx)
    return {"data": container.capabilities.listings(catalogue)}


@router.get(
    "/tools",
    operation_id="list_model_tools",
    summary="The operations the model can actually call this turn",
    responses=_AUTHED,
    description=(
        "Names, one-line descriptions, and which capabilities were deferred. Pass a "
        "session id to include anything that session has already bound with "
        "`capabilities.use`."
    ),
)
async def list_model_tools(
    acting: ActingAsDep,
    container: ContainerDep,
    store: StoreDep,
    session_id: Annotated[str | None, Query(max_length=64)] = None,
    profile: Annotated[str, Query(min_length=1, max_length=128)] = "personal",
) -> dict[str, Any]:
    """The bound registry, not the whole catalogue."""
    resolved_profile = profile
    session = session_id or ""
    row: dict[str, Any] = {}
    if session_id is not None:
        row = await store.get(acting.account_id, session_id)
        resolved_profile = str(row["profile"])
    pack_ctx = container.pack_context(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile=resolved_profile,
            session_id=session,
            incognito=_incognito(row),
        )
    )
    _attach_workspace(pack_ctx, row)
    catalogue = await container.capabilities.probe(pack_ctx)
    return container.capabilities.tools(catalogue, session)


@router.post(
    "/tools/{name}/invoke",
    operation_id="invoke_tool",
    summary="Run one tool without starting a model turn",
    responses=_INVOKE,
    description=(
        "The same approval policy as a conversation, and no tokens burned. A write that "
        "still needs a person is 409; grant it through `/v1/permissions` or answer the "
        "ask on the session, then retry."
    ),
)
async def invoke_tool(
    acting: ActingAsDep,
    container: ContainerDep,
    store: StoreDep,
    name: str,
    body: InvokeBody,
) -> dict[str, Any]:
    profile = body.profile
    session = body.session_id
    mode = "ask"
    row: dict[str, Any] = {}
    if session:
        row = await store.get(acting.account_id, session)
        profile = str(row["profile"])
        mode = str(row["permission_mode"])
    pack_ctx = container.pack_context(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile=profile,
            session_id=session,
            permission_mode=mode,
            incognito=_incognito(row),
        )
    )
    _attach_workspace(pack_ctx, row)
    pack_ctx.grants = await grants_for(store, acting.account_id, profile, session_id=session)
    result = await container.capabilities.invoke(name, body.input, pack_ctx)
    return {"tool": name, "steps": result.get("steps") or [], "text": result.get("text") or ""}


def _incognito(row: dict[str, Any]) -> bool:
    """Whether the session a call is scoped to promised to leave memory alone.

    A promise about the conversation, not about who is calling: a client's own tool call in
    an incognito session reads and writes memory no more than the model may. Both routes
    built their context without it, so `notes.remember` scoped to an incognito session wrote
    a memory, and `notes.search` read the person's.
    """
    return bool(row.get("incognito", 0))


def _attach_workspace(pack_ctx: PackContext, row: dict[str, Any]) -> None:
    """The session's own workspace, so its tools are the ones its turns can call.

    A turn gets this from `prepare_turn`. A direct call scoped to the same session has to
    get it too, or the workspace probes as "no workspace is attached" and its operations are
    neither listed nor invokable: a session's own files were unreachable from here. With no
    session there is no row, and nothing to attach.
    """
    environment_id = str(row.get("workspace_environment_id") or "")
    if environment_id:
        pack_ctx.workspace_environment_id = environment_id
        pack_ctx.workspace_path = WorkspaceScope(environment_id, str(row["id"])).root
