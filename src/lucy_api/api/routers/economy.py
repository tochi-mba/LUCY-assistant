"""Compaction, usage and durable results, as session sub-resources.

Literal paths live here rather than on the session router so they cannot be swallowed by
``/sessions/{id}``. Routers never import weftai; result resolution happens one layer down.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, CurrentCallerDep, StoreDep
from lucy_api.api.preview import session_view
from lucy_api.api.schemas.files import ArtifactPage
from lucy_api.api.schemas.problem import Problem
from lucy_api.api.schemas.sessions import SelectionDep
from lucy_api.core.container import PackRequest
from lucy_api.core.errors import LucyError
from lucy_api.sessions.compact import (
    MAX_KEEP_RECENT_TURNS,
    compact_session,
    list_compactions,
    uncompact_session,
)
from lucy_api.sessions.results import get_result, list_results, resolve_result
from lucy_api.sessions.usage import session_usage
from lucy_api.turn.prompt import SessionView, window_report

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


class CompactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_recent_turns: int | None = Field(
        default=None,
        ge=0,
        le=MAX_KEEP_RECENT_TURNS,
        description=(
            "How many of the newest turns stay verbatim. Omitted, the person's "
            "`history_turns_kept` setting decides, as it does for automatic compaction."
        ),
    )


@router.post(
    "/{session_id}/compact",
    operation_id="compact_session",
    summary="Summarise older turns now, without rewriting them",
    responses=_ADDRESSED,
    description=(
        "Writes an extractive summary covering everything older than the newest turns, and "
        "says how full the window was before and is after (`context_before`, "
        "`context_after`, shaped like `GET /context/window`). The transcript stays; "
        "`uncompact` restores it. Automatic compaction still runs on its own; this is the "
        "same thing, asked for by a person, whenever they like. After three automatic "
        "failures only this route still tries, and a success switches automatic compaction "
        "back on. Asking again with nothing new to cover is a 409."
    ),
)
async def compact(
    acting: ActingAsDep,
    container: ContainerDep,
    session_id: SessionId,
    body: CompactBody | None = None,
) -> dict[str, Any]:
    session = await container.store.get(acting.account_id, session_id)
    policy = await container.lucy_policy(acting.token, str(session["profile"]))
    keep = policy.history_turns_kept
    if body is not None and body.keep_recent_turns is not None:
        keep = body.keep_recent_turns
    view = await _gauge_view(acting, container, session_id)
    before = window_report(view) if view is not None else None
    try:
        written = await compact_session(
            container.store,
            acting.account_id,
            session_id,
            keep_recent=keep,
            trigger="manual",
            trigger_tokens=before["used_tokens"] if before is not None else 0,
        )
    finally:
        # The events went in with the row, in one transaction; a client following the
        # conversation hears about them now rather than whenever the next turn ends.
        await container.events.publish_persisted(session_id)
    after = None
    if view is not None:
        rows = await container.store.records(acting.account_id, session_id, "compactions")
        after = window_report(replace(view, compactions=rows))
    return {**written, "keep_recent_turns": keep, "context_before": before, "context_after": after}


async def _gauge_view(
    acting: ActingAsDep, container: ContainerDep, session_id: str
) -> SessionView | None:
    """The view the before-and-after figures are read from, if one can be built.

    A person compacting does not need the turn machinery: compaction reads the transcript
    and nothing else. If the view cannot be built -- settings unreachable, say -- the
    compaction still happens and the figures are `null` rather than invented.
    """
    try:
        return await session_view(acting, container, session_id)
    except LucyError:
        return None


@router.get(
    "/{session_id}/compactions",
    operation_id="list_session_compactions",
    summary="Every compaction this conversation has had, newest first",
    responses=_ADDRESSED,
    description=(
        "Each row says who asked for it (`trigger`: `manual` or `auto`), how full the window "
        "was then (`trigger_tokens`), the range it covers, whether it is still `active`, and "
        "whether it is the one the model is reading (`shown`; an active row a newer one "
        "overlaps is superseded). `automatic` is false after three consecutive failures."
    ),
)
async def compactions(
    caller: CurrentCallerDep, store: StoreDep, session_id: SessionId
) -> dict[str, Any]:
    return await list_compactions(store, caller.account_id, session_id)


@router.get(
    "/{session_id}/context/window",
    operation_id="get_session_context_window",
    summary="How full this conversation's window is",
    responses=_ADDRESSED,
    description=(
        "The figure the model is told and that warnings and compaction act on: tokens used "
        "of the window, the percentage, where the warning and automatic compaction sit, how "
        "many tokens are left before it runs, how many opening turns are read as a summary, "
        "and `state` (`ok`, `warning`, `compacting`, `over`). The cheap half of "
        "`GET /context`: no prompt text."
    ),
)
async def context_window(
    acting: ActingAsDep, container: ContainerDep, session_id: SessionId
) -> dict[str, Any]:
    view = await session_view(acting, container, session_id)
    listed = await list_compactions(container.store, acting.account_id, session_id)
    return {
        **window_report(view),
        "automatic_compaction": listed["automatic"],
        "compactions": sum(1 for row in listed["data"] if row["shown"]),
    }


@router.post(
    "/{session_id}/uncompact",
    operation_id="uncompact_session",
    summary="Restore turns a compaction was standing in for",
    responses=_ADDRESSED,
    description="Deactivates one compaction row. The items it covered are projected again.",
)
async def uncompact(
    caller: CurrentCallerDep,
    container: ContainerDep,
    session_id: SessionId,
    body: UncompactBody,
) -> dict[str, Any]:
    undone = await uncompact_session(container.store, caller.account_id, session_id, body.id)
    await container.events.publish_persisted(session_id)
    return undone


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
