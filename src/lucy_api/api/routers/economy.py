"""Compaction, usage and durable results, as session sub-resources.

Literal paths live here rather than on the session router so they cannot be swallowed by
``/sessions/{id}``. Routers never import weftai; result resolution happens one layer down.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, CurrentCallerDep, StoreDep
from lucy_api.api.schemas.files import ArtifactPage
from lucy_api.api.schemas.problem import Problem
from lucy_api.api.schemas.sessions import SelectionDep
from lucy_api.core.container import PackRequest
from lucy_api.sessions.compact import compact_session, uncompact_session
from lucy_api.sessions.results import get_result, list_results, resolve_result
from lucy_api.sessions.usage import session_usage

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_409_CONFLICT: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}
SessionId = Annotated[str, Path(min_length=1, max_length=64)]


class UncompactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="The compaction to deactivate.")


@router.post(
    "/{session_id}/compact",
    operation_id="compact_session",
    summary="Summarise older turns without rewriting them",
    responses=_ADDRESSED,
    description=(
        "Writes an extractive summary covering everything older than the newest turns the "
        "person asked to keep. The transcript stays; deactivating the row restores it. "
        "Three consecutive failures disable compaction for the session."
    ),
)
async def compact(
    acting: ActingAsDep, container: ContainerDep, session_id: SessionId
) -> dict[str, Any]:
    session = await container.store.get(acting.account_id, session_id)
    policy = await container.lucy_policy(acting.token, str(session["profile"]))
    return await compact_session(
        container.store,
        acting.account_id,
        session_id,
        keep_recent=policy.history_turns_kept,
    )


@router.post(
    "/{session_id}/uncompact",
    operation_id="uncompact_session",
    summary="Restore turns a compaction was standing in for",
    responses=_ADDRESSED,
    description="Deactivates one compaction row. The items it covered are projected again.",
)
async def uncompact(
    caller: CurrentCallerDep, store: StoreDep, session_id: SessionId, body: UncompactBody
) -> dict[str, Any]:
    return await uncompact_session(store, caller.account_id, session_id, body.id)


@router.get(
    "/{session_id}/usage",
    operation_id="get_session_usage",
    summary="Token and cost totals for this conversation",
    responses=_ADDRESSED,
    description="Reads the sums the turn loop already wrote. There is no second counter.",
)
async def usage(caller: CurrentCallerDep, store: StoreDep, session_id: SessionId) -> dict[str, Any]:
    return await session_usage(store, caller.account_id, session_id)


@router.get(
    "/{session_id}/results",
    operation_id="list_session_results",
    summary="Tool results still addressable by reference",
    responses=_ADDRESSED,
    description=(
        "Summaries of live `$id` values. Pass `ref` to resolve `$hits` or `$hits[2]` "
        "without asking the model to fetch the page again."
    ),
)
async def results(
    caller: CurrentCallerDep,
    store: StoreDep,
    session_id: SessionId,
    ref: Annotated[str | None, Query(max_length=256)] = None,
) -> dict[str, Any]:
    if ref:
        return await resolve_result(store, caller.account_id, session_id, ref)
    return {"data": await list_results(store, caller.account_id, session_id)}


@router.get(
    "/{session_id}/results/{result_id}",
    operation_id="get_session_result",
    summary="One stored tool result",
    responses=_ADDRESSED,
    description="The payload behind one `$id`. Expired or evicted results are 404.",
)
async def one_result(
    caller: CurrentCallerDep,
    store: StoreDep,
    session_id: SessionId,
    result_id: Annotated[str, Path(min_length=1, max_length=128)],
) -> dict[str, Any]:
    return await get_result(store, caller.account_id, session_id, result_id)


@router.get(
    "/{session_id}/artifacts",
    operation_id="list_session_artifacts",
    summary="Files this conversation produced",
    response_model=ArtifactPage,
    responses=_ADDRESSED,
    description=(
        "Artifacts belong to the session and are deleted with it. Uploaded files live at "
        "`/v1/files` and outlive the conversation."
    ),
)
async def list_artifacts(
    caller: CurrentCallerDep,
    container: ContainerDep,
    session_id: SessionId,
    selection: SelectionDep,
) -> ArtifactPage:
    page = await container.blobs.list_artifacts(caller.account_id, session_id, selection)
    return ArtifactPage.model_validate(page)


def _request(acting: ActingAsDep, session: dict[str, Any]) -> PackRequest:
    return PackRequest(
        caller=acting.caller,
        user_token=acting.token,
        profile=str(session["profile"]),
        session_id=str(session["id"]),
        permission_mode=str(session.get("permission_mode") or "ask"),
        incognito=bool(session.get("incognito")),
    )


@router.get(
    "/{session_id}/memory",
    operation_id="get_session_memory",
    summary="The topic index currently eligible for this conversation",
    responses=_ADDRESSED,
    description=(
        "What the live-state block would show: trusted topics, held-back untrusted ones "
        "omitted. Incognito sessions return an empty list rather than a differently shaped "
        "miss. This is a projection of Memory-api, not a second store."
    ),
)
async def session_memory(
    acting: ActingAsDep, container: ContainerDep, session_id: SessionId
) -> dict[str, Any]:
    session = await container.store.get(acting.account_id, session_id)
    return await container.session_memory(_request(acting, session), session)


@router.get(
    "/{session_id}/workspace",
    operation_id="get_session_workspace",
    summary="The confined directory attached to this conversation",
    responses=_ADDRESSED,
    description=(
        "Returns the environment id and the session-relative path. The host path never "
        "leaves the sandbox. `status` is `attached` or `missing`."
    ),
)
async def session_workspace(
    caller: CurrentCallerDep, container: ContainerDep, session_id: SessionId
) -> dict[str, Any]:
    session = await container.store.get(caller.account_id, session_id)
    return container.workspace_view(session)


@router.post(
    "/{session_id}/workspace",
    operation_id="attach_session_workspace",
    summary="Attach a confined directory if this conversation does not have one",
    responses=_ADDRESSED,
    description="Idempotent. A session that already has a workspace is returned unchanged.",
)
async def attach_session_workspace(
    acting: ActingAsDep, container: ContainerDep, session_id: SessionId
) -> dict[str, Any]:
    session = await container.store.get(acting.account_id, session_id)
    attached = await container.ensure_workspace(_request(acting, session), session)
    return container.workspace_view(attached)


@router.post(
    "/{session_id}/workspace/reset",
    operation_id="reset_session_workspace",
    summary="Wipe the session directory and seed it again",
    responses=_ADDRESSED,
    description=(
        "Deletes the confined subtree and rewrites `progress.md` and `tasks.json`. The "
        "account's environment is shared and is not destroyed."
    ),
)
async def reset_session_workspace(
    acting: ActingAsDep, container: ContainerDep, session_id: SessionId
) -> dict[str, Any]:
    session = await container.store.get(acting.account_id, session_id)
    return await container.reset_workspace(_request(acting, session), session_id)
