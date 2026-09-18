"""The session surface: what a conversation is, and how to read one back.

Every ``operation_id`` here is public API. They become MCP tool names, so renaming one breaks
every client with a tool bound to it, and a contract test pins the exact set rather than a
minimum.

**No route accepts an account.** Whose conversation this is comes from the verified token and
from nowhere else, so a cross-account read is not refused by a check -- it is inexpressible,
because no handler has anywhere to put another person's id.

**Reading is cursor-only.** ``limit``, ``order``, ``after`` and ``before``. A transcript is
append-only and grows while it is being paged, and offset paging over a growing collection
both duplicates rows and skips them, which on a conversation looks like the assistant said
something twice and then forgot a message.

**The write path is not here.** ``POST /v1/sessions/{id}/inputs`` -- the one place new work
enters a session -- belongs to the turn loop, and this module leaves it a seam rather than a
stub: :func:`lucy_api.sessions.turns.open_turn` records the turn, this router's
``IdempotencyKeyDep`` and ``SelectionDep`` are the argument shapes it should reuse, and
``POST /v1/turns/{id}/cancel`` below is already the other end of that conversation. What is
here is everything a client needs to create a session, read it, branch it and stop it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Header, Path, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from lucy_api.api.dependencies import (
    ActingAsDep,
    ContainerDep,
    CurrentCallerDep,
    IdempotencyKeyDep,
    StoreDep,
    StreamCursorDep,
)
from lucy_api.api.schemas.problem import Problem
from lucy_api.api.schemas.sessions import (
    ItemResource,
    Page,
    SelectionDep,
    SessionResource,
    TurnResource,
)
from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError as ClientDownstreamError
from lucy_api.core.container import PackRequest
from lucy_api.core.errors import LucyError, conflict
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportDownstreamError
from lucy_api.permissions.approvals import answer_approval
from lucy_api.sessions.fork import fork_session as fork_the_session
from lucy_api.sessions.items import list_items
from lucy_api.sessions.models import CreateSession, ForkSession, InputBatch, UpdateSession
from lucy_api.sessions.turns import cancel_turn as request_cancellation
from lucy_api.sessions.turns import list_turns, submit_messages
from lucy_api.stream import ai_sdk, sse
from lucy_api.turn.prompt import SessionView, context_for_session, conversation_order, view_limits

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(prefix="/v1", tags=["sessions"])

ONE_APPROVAL = "Answer one approval at a time."
OWNING_WORKFLOW = "This input needs the approval or connection workflow that owns it."

_PROBLEM: dict[str, Any] = {"model": Problem}

# Declaring 422 explicitly is not decoration. FastAPI inserts its own `HTTPValidationError`
# response for any route with a parameter or a body unless 422 is already declared, and that
# document describes a body this service never sends: every failure here is a problem
# document. A published contract that is wrong about the error shape is worse than a silent
# one, because a client writes a parser against it.
_VALIDATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    **_VALIDATED,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
}
_IDEMPOTENT: dict[int | str, dict[str, Any]] = {
    **_VALIDATED,
    status.HTTP_409_CONFLICT: _PROBLEM,
}
_WORKSPACE_WRITES: dict[int | str, dict[str, Any]] = {
    **_IDEMPOTENT,
    status.HTTP_503_SERVICE_UNAVAILABLE: _PROBLEM,
}
WORKSPACE_UNAVAILABLE = "workspace-unavailable"

SessionIdPath = Annotated[str, Path(description="The session's id.", max_length=64)]
ItemIdPath = Annotated[str, Path(description="The item's id.", max_length=64)]
TurnIdPath = Annotated[str, Path(description="The turn's id.", max_length=64)]

NOT_YOURS = (
    "A session belonging to another account answers 404, exactly as one that never existed "
    "does. An id is unguessable, so telling the two apart would be the only way to confirm "
    "that somebody else's conversation exists."
)
CURSORS = (
    "Cursor paging only. Pass `after` the last id you saw to continue; `has_more` says "
    "whether there is another page. A cursor that is not in the collection is a 404, not an "
    "empty page: a stale bookmark and an exhausted collection need different answers."
)


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_session",
    summary="Start a conversation",
    response_model=SessionResource,
    responses=_WORKSPACE_WRITES,
    description=(
        "Creates an empty session for the person whose token this is. Nothing has been said "
        "yet and nothing has been spent.\n\n"
        "`Idempotency-Key` is required. Retrying with the same key returns the session that "
        "was already created rather than a second one; sending the same key with a different "
        "body is a 409, because that is a bug in the caller rather than a retry.\n\n"
        "`input_policy` decides what happens when a second message arrives while a turn is "
        "running, and `permission_mode` decides how much can happen without being asked. "
        "Both can be changed later. An isolated workspace is provisioned and attached "
        "before this request succeeds, including `progress.md`, `tasks.json`, and a git "
        "baseline when the sandbox can run git."
    ),
)
async def create_session(
    request: CreateSession,
    idempotency_key: IdempotencyKeyDep,
    acting: ActingAsDep,
    container: ContainerDep,
) -> SessionResource:
    """Create one session and its isolated workspace for the verified caller."""
    request = await container.apply_create_defaults(request, acting.token)
    row = await container.store.create(acting.account_id, request, idempotency_key)
    row = await _ensure_workspace(acting, container, row)
    return SessionResource.model_validate(row)


@router.get(
    "/sessions",
    operation_id="list_sessions",
    summary="List this person's conversations",
    response_model=Page[SessionResource],
    responses=_ADDRESSED,
    description=(
        "Every session this token's account owns, oldest first by default. Archived sessions "
        "are included and carry an `archived_at`; they are hidden by clients, not by the "
        "API, because a person looking for something they archived is the main reason to "
        "ask.\n\n" + CURSORS
    ),
)
async def list_sessions(
    selection: SelectionDep, caller: CurrentCallerDep, store: StoreDep
) -> Page[SessionResource]:
    """Page through the caller's own sessions."""
    raw = await store.list_sessions(
        caller.account_id, selection.limit, selection.after, selection.before, selection.order
    )
    return Page[SessionResource].model_validate(raw)


@router.get(
    "/sessions/{session_id}",
    operation_id="get_session",
    summary="Read one conversation's state",
    response_model=SessionResource,
    responses=_ADDRESSED,
    description=(
        "The session itself -- model, persona, policies, workspace, spend -- and not its "
        "transcript. Use `list_session_items` for what was said.\n\n" + NOT_YOURS
    ),
)
async def get_session(
    session_id: SessionIdPath, caller: CurrentCallerDep, store: StoreDep
) -> SessionResource:
    """Return one session belonging to the verified caller."""
    return SessionResource.model_validate(await store.get(caller.account_id, session_id))


@router.patch(
    "/sessions/{session_id}",
    operation_id="update_session",
    summary="Rename a conversation, or change how it behaves",
    response_model=SessionResource,
    responses=_ADDRESSED,
    description=(
        "Only the four settings a person can reasonably change mid-conversation: the title, "
        "the double-text policy, the permission mode, and whether it is archived. The model "
        "and the thinking configuration are fixed for the life of a session -- changing them "
        "halfway would make the transcript a record of two different assistants.\n\n"
        "Omitted fields are left alone. Archiving is reversible: `archived: false` brings it "
        "back with everything intact."
    ),
)
async def update_session(
    session_id: SessionIdPath,
    request: UpdateSession,
    caller: CurrentCallerDep,
    store: StoreDep,
) -> SessionResource:
    """Apply only the fields the caller actually supplied."""
    changes = request.model_dump(exclude_unset=True)
    row = await store.update(caller.account_id, session_id, changes)
    return SessionResource.model_validate(row)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_session",
    summary="Delete a conversation and its transcript",
    responses=_ADDRESSED,
    description=(
        "Takes the session, its items, its turns and its events with it, and leaves an audit "
        "row saying it happened. What survives is memory: things Lucy learned during the "
        "conversation are facts about a person rather than part of a transcript, and they "
        "keep the provenance id pointing here until erasure takes them too.\n\n"
        "The session's confined workspace directory goes with it. The account's "
        "environment is shared and stays. Archiving is the reversible option. This one is not."
    ),
)
async def delete_session(
    session_id: SessionIdPath, acting: ActingAsDep, container: ContainerDep
) -> Response:
    """Delete one session belonging to the verified caller."""
    row = await container.store.get(acting.account_id, session_id)
    await container.discard_session(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile=str(row["profile"]),
            session_id=session_id,
        ),
        session_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sessions/{session_id}/fork",
    status_code=status.HTTP_201_CREATED,
    operation_id="fork_session",
    summary="Branch a conversation at a point in its transcript",
    response_model=SessionResource,
    responses={**_ADDRESSED, status.HTTP_503_SERVICE_UNAVAILABLE: _PROBLEM},
    description=(
        "Creates a new session holding a copy of this one up to `item_id`, or all of it when "
        "`item_id` is omitted. The copies get new ids and their parent links are rewritten to "
        "match, so the fork is a conversation in its own right rather than a view of this "
        "one: deleting either leaves the other whole.\n\n"
        "**The workspace is isolated.** The fork never shares the parent's files. A fresh "
        "environment is provisioned for it before this request succeeds.\n\n"
        "The fork starts idle with its spend at zero, and remembers where it came from in "
        "`parent_session_id` and `forked_from_item`."
    ),
)
async def fork_session(
    session_id: SessionIdPath,
    request: ForkSession,
    acting: ActingAsDep,
    container: ContainerDep,
) -> SessionResource:
    """Copy a session's transcript into a new session, remapping every id."""
    row = await fork_the_session(container.store, acting.account_id, session_id, request.item_id)
    row = await _ensure_workspace(acting, container, row)
    return SessionResource.model_validate(row)


async def _ensure_workspace(
    acting: ActingAsDep, container: ContainerDep, row: dict[str, Any]
) -> dict[str, Any]:
    """Turn sibling provisioning failures into Lucy's stable public problem document."""
    request = PackRequest(
        caller=acting.caller,
        user_token=acting.token,
        profile=str(row["profile"]),
        session_id=str(row["id"]),
        permission_mode=str(row.get("permission_mode", "ask")),
        incognito=bool(row.get("incognito", 0)),
    )
    try:
        return await container.ensure_workspace(request, row)
    except (
        ClientDownstreamError,
        ExchangeError,
        NoBrokerError,
        TransportDownstreamError,
    ) as exc:
        message = "The session workspace could not be provisioned; retry when it is available."
        raise LucyError(WORKSPACE_UNAVAILABLE, message, 503) from exc


@router.post(
    "/sessions/{session_id}/inputs",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="submit_session_input",
    summary="Send the next message to a conversation",
    response_model=TurnResource,
    responses=_IDEMPOTENT,
    description=(
        "The one write path for a conversation. A message is appended to the transcript and "
        "a queued turn is created atomically, then the response names the turn a client can "
        "poll or follow on the event stream. Closing the client connection does not cancel it.\n\n"
        "`Idempotency-Key` is required: retrying returns the original turn without appending "
        "the message twice. `input.message` starts a turn. `input.approval` answers a parked "
        "write; the client's `approved` flag is an input, and the gate re-checks the grant "
        "before the tool runs. Tool results, connection replies and cancellations enter "
        "through the same envelope once their owning workflows are enabled."
    ),
)
async def submit_session_input(
    session_id: SessionIdPath,
    request: InputBatch,
    idempotency_key: IdempotencyKeyDep,
    acting: ActingAsDep,
    container: ContainerDep,
) -> JSONResponse:
    """Append a person's message and make the turn durable before it can run."""
    session = await container.store.get(acting.account_id, session_id)
    prepared = await container.prepare_turn(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile=str(session["profile"]),
            session_id=session_id,
            permission_mode=str(session.get("permission_mode", "ask")),
            incognito=bool(session.get("incognito", 0)),
        ),
        session,
    )
    events = [event.model_dump() for event in request.events]
    kinds = {event["type"] for event in events}
    if kinds == {"input.approval"}:
        if len(events) != 1:
            raise conflict(ONE_APPROVAL)
        row = (
            await answer_approval(
                container.store, acting.account_id, session_id, events[0], idempotency_key
            )
        ).turn
    elif kinds == {"input.message"}:
        row = await submit_messages(
            container.store, acting.account_id, session_id, events, idempotency_key
        )
    else:
        raise conflict(OWNING_WORKFLOW)
    if row["status"] == "queued":
        container.turns.authorize(str(row["id"]), prepared)
    # The turn transaction writes its own audit events. Fan those exact committed rows out
    # rather than adding a second event that merely says the same thing.
    await container.events.publish_persisted(session_id)
    container.turns.wake()
    resource = TurnResource.model_validate(row)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=resource.model_dump(mode="json"),
        headers={"Location": f"/v1/turns/{row['id']}"},
    )


@router.get(
    "/sessions/{session_id}/events",
    operation_id="stream_session_events",
    summary="Follow a conversation as it changes",
    responses=_ADDRESSED,
    description=(
        "Sends a complete state snapshot and then ordered events as server-sent events. "
        "A lost connection does not stop the turn. Reconnect with `starting_after` set to "
        "the last `sequence_number` handled; it wins over `Last-Event-ID` when both are "
        "present because it is chosen by the client rather than replayed by a browser."
    ),
)
async def stream_session_events(
    session_id: SessionIdPath,
    caller: CurrentCallerDep,
    store: StoreDep,
    container: ContainerDep,
    stream_cursor: StreamCursorDep,
    accept: Annotated[str | None, Header()] = None,
    ui_stream: Annotated[str | None, Header(alias="x-lucy-ui-message-stream")] = None,
) -> StreamingResponse:
    """Open an authorized, resumable read stream without affecting turn execution."""
    await store.get(caller.account_id, session_id)
    cursor = sse.resume_from(stream_cursor.starting_after, stream_cursor.last_event_id)
    as_ui = ai_sdk.negotiated(accept, ui_stream)

    async def stream() -> AsyncIterator[str]:
        async with container.events.subscribe(session_id, starting_after=cursor) as subscriber:
            if as_ui:
                async for frame in ai_sdk.frames(subscriber):
                    yield frame
            else:
                async for frame in sse.frames(subscriber):
                    yield frame

    headers = ai_sdk.RESPONSE_HEADERS if as_ui else sse.HEADERS
    return StreamingResponse(stream(), media_type=sse.MEDIA_TYPE, headers=headers)


@router.get(
    "/sessions/{session_id}/items",
    operation_id="list_session_items",
    summary="Read a conversation's transcript",
    response_model=Page[ItemResource],
    responses=_ADDRESSED,
    description=(
        "Every item in the session: messages, reasoning, plans, tool calls and their results, "
        "approval requests and answers, compactions, artifacts and errors. Approvals and "
        "elicitations are items rather than a side channel, so a client that renders the "
        "transcript renders them too and an audit of what was asked and answered is a read "
        "of one collection.\n\n"
        "`seq` is the order things happened in. `parent_id` is what each item answers, and "
        "two items can share one: that is an edit and its regeneration, not a "
        "duplicate.\n\n" + CURSORS
    ),
)
async def list_session_items(
    session_id: SessionIdPath,
    selection: SelectionDep,
    caller: CurrentCallerDep,
    store: StoreDep,
) -> Page[ItemResource]:
    """Page through one session's transcript."""
    raw = await list_items(store, caller.account_id, session_id, selection)
    return Page[ItemResource].model_validate(raw)


@router.get(
    "/sessions/{session_id}/turns",
    operation_id="list_session_turns",
    summary="List the units of work in a conversation",
    response_model=Page[TurnResource],
    responses=_ADDRESSED,
    description=(
        "One row per turn, with how it ended and what it cost. This is what a client polls "
        "when it wants progress without subscribing to the event stream.\n\n" + CURSORS
    ),
)
async def list_session_turns(
    session_id: SessionIdPath,
    selection: SelectionDep,
    caller: CurrentCallerDep,
    store: StoreDep,
) -> Page[TurnResource]:
    """Page through one session's turns."""
    raw = await list_turns(store, caller.account_id, session_id, selection)
    return Page[TurnResource].model_validate(raw)


@router.get(
    "/items/{item_id}",
    operation_id="get_item",
    summary="Read one item",
    response_model=ItemResource,
    responses=_ADDRESSED,
    description=(
        "One item, addressed directly rather than through the session it belongs to, because "
        "that is how an id arrives -- from an event, a citation or a link somebody "
        "sent.\n\n" + NOT_YOURS
    ),
)
async def get_item(item_id: ItemIdPath, caller: CurrentCallerDep, store: StoreDep) -> ItemResource:
    """Return one item belonging to the verified caller."""
    return ItemResource.model_validate(await store.item(caller.account_id, item_id))


@router.get(
    "/turns/{turn_id}",
    operation_id="get_turn",
    summary="Read one turn",
    response_model=TurnResource,
    responses=_ADDRESSED,
    description=(
        "The turn's status, how it terminated, what the provider said about its own "
        "generation, and what it spent. This is the call a webhook tells you to make: a "
        "webhook carries a signal, never a payload, so results stay out of third-party "
        "logs.\n\n" + NOT_YOURS
    ),
)
async def get_turn(turn_id: TurnIdPath, caller: CurrentCallerDep, store: StoreDep) -> TurnResource:
    """Return one turn belonging to the verified caller."""
    return TurnResource.model_validate(await store.turn(caller.account_id, turn_id))


@router.post(
    "/turns/{turn_id}/cancel",
    operation_id="cancel_turn",
    summary="Stop a turn",
    response_model=TurnResource,
    responses=_ADDRESSED,
    description=(
        "Idempotent, and safe to call on a turn that has already finished -- that answers "
        "with the turn as it stands rather than an error, because the honest response to "
        "'stop this' when it is already stopped is what state it stopped in.\n\n"
        "A turn that is actually running is asked to stop rather than killed: "
        "`cancel_requested` goes true and the turn ends itself, so the work it had already "
        "done survives. A turn that is queued, or waiting on an approval or a credential, "
        "has nothing in flight and is cancelled immediately.\n\n"
        "Dropping the event stream does **not** cancel anything. This call is the only thing "
        "that does, and it only ever affects the turn it names."
    ),
)
async def cancel_turn(
    turn_id: TurnIdPath,
    caller: CurrentCallerDep,
    store: StoreDep,
    container: ContainerDep,
) -> TurnResource:
    """Request cancellation of one turn belonging to the verified caller."""
    row = await request_cancellation(store, caller.account_id, turn_id)
    container.turns.discard(turn_id)
    return TurnResource.model_validate(row)


@router.get(
    "/sessions/{session_id}/context",
    operation_id="get_session_context",
    summary="The exact prompt this session would send, with per-band token counts",
    responses=_ADDRESSED,
    description=(
        "What the model would see on the next turn, priced band by band. A surprising "
        "answer is usually a surprising prompt, and this is the document that makes that "
        "inspectable without spending another generation."
    ),
)
async def get_session_context(
    session_id: SessionIdPath,
    acting: ActingAsDep,
    store: StoreDep,
    container: ContainerDep,
) -> dict[str, Any]:
    """Assemble this session's prompt without running a turn."""
    session = await store.get(acting.account_id, session_id)
    items = await store.records(acting.account_id, session_id, "items")
    compact = await store.records(acting.account_id, session_id, "compactions")
    turns = await store.records(acting.account_id, session_id, "turns")
    prepared = await container.prepare_turn(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile=str(session["profile"]),
            session_id=session_id,
            permission_mode=str(session.get("permission_mode", "ask")),
            incognito=bool(session.get("incognito", 0)),
        ),
        session,
    )
    catalogue = await container.capabilities.probe(prepared.pack_context)
    ready = tuple(item.pack.id for item in catalogue.ready())
    visible = {str(turn["id"]) for turn in turns}
    parent_items = [row for row in items if not row.get("agent_id")]
    policy = prepared.pack_context.policy
    return await context_for_session(
        SessionView(
            session_id=session_id,
            items=conversation_order(parent_items, turns, visible),
            capabilities=ready,
            session=session,
            compactions=compact,
            turn_number=sum(1 for turn in turns if turn["status"] == "completed") + 1,
            live=prepared.live,
            response_style=policy.response_style,
            **view_limits(policy),
        )
    )
