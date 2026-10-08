"""The HTTP face: six routes, one verifier, sentences for every refusal.

No ``from __future__ import annotations`` here, deliberately: the caller dependency is a
closure, and postponed annotations would leave its name unresolvable when FastAPI reads
the route signatures -- every ``who: Caller`` silently became a required query parameter.
"""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lucy_coder.auth import (
    AuthenticationError,
    KeyringUnreachableError,
    TokenVerifier,
    VerifiedCaller,
)
from lucy_coder.config import Settings
from lucy_coder.runner import doctor
from lucy_coder.service import CoderService, RefusedError

BRIEF_CHARS_MAX = 20_000
TITLE_CHARS_MAX = 120
MESSAGE_CHARS_MAX = 20_000


class NewTask(BaseModel):
    brief: str = Field(min_length=1, max_length=BRIEF_CHARS_MAX)
    directory: str = Field(min_length=1, max_length=1_024)
    run_level: str = "edits"
    title: str = Field(default="", max_length=TITLE_CHARS_MAX)
    model: str = Field(default="", max_length=64)


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=MESSAGE_CHARS_MAX)
    mode: str = Field(default="", max_length=16)
    allow_tools: list[str] = Field(default_factory=list, max_length=10)


def create_app(settings: Settings, service: CoderService, verifier: TokenVerifier) -> FastAPI:
    """The app, from parts the caller already built -- tests hand in fakes the same way."""
    app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None)

    async def caller(
        authorization: Annotated[str, Header()] = "",
    ) -> VerifiedCaller:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError
        return await verifier.verify(token.strip())

    Caller = Annotated[VerifiedCaller, Depends(caller)]  # noqa: N806 - a type alias

    @app.exception_handler(RefusedError)
    async def refused(_request: Request, exc: RefusedError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.exception_handler(AuthenticationError)
    async def refused_token(_request: Request, _exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": "a coder-api token is required"})

    @app.exception_handler(KeyringUnreachableError)
    async def keys_down(_request: Request, _exc: KeyringUnreachableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "keyring is unreachable"})

    @app.get("/healthy")
    async def healthy() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, Any]:
        """Whether a delegation would work right now: the CLI answers, or why not."""
        trouble = doctor(settings.claude_command)
        if trouble:
            response.status_code = 503
        return {"status": "ok" if not trouble else "unavailable", "detail": trouble}

    @app.post("/v1/tasks", status_code=201)
    async def start(body: NewTask, who: Caller) -> dict[str, Any]:
        task = await service.start(
            account_id=who.account_id,
            brief=body.brief,
            directory=body.directory,
            run_level=body.run_level,
            title=body.title,
            model=body.model,
        )
        return task.public()

    @app.get("/v1/tasks")
    async def listing(who: Caller) -> dict[str, Any]:
        return {"tasks": await service.list(who.account_id)}

    @app.get("/v1/tasks/{task_id}")
    async def read(
        task_id: str,
        who: Caller,
        tail_chars: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await service.get(
            who.account_id, task_id, tail_chars=min(tail_chars, settings.tail_chars_max)
        )

    @app.post("/v1/tasks/{task_id}/message")
    async def message(task_id: str, body: Message, who: Caller) -> dict[str, Any]:
        task, advice = await service.message(
            who.account_id,
            task_id,
            body.text,
            mode=body.mode,
            allow_tools=tuple(body.allow_tools),
        )
        return {**task.public(), "advice": advice}

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str, who: Caller) -> dict[str, Any]:
        task = await service.cancel(who.account_id, task_id)
        return task.public()

    return app
