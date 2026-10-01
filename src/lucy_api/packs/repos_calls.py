"""What each `repos` operation does, behind the pack that declares it (`packs/repos.py`).

The pack is the contract the model and the gate see: names, descriptions, inputs, effects and
permissions. This module is the other half -- what a call does with the client and how its
answer is projected -- kept apart so neither half is long enough to stop being read.

Every projection takes named fields only, so whatever the service starts sending tomorrow
cannot reach a prompt by accident.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from weftai.schema.spec import string_schema

from lucy_api.clients.repos import DEFAULT_LIMIT, MAX_LIMIT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from weftai.operation import RunContext

    from lucy_api.clients.repos import CheckRun, Issue, Pull, Repo, ReposClient
    from lucy_api.packs.context import PackContext

REPO_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"
"""`owner/name`, as GitHub spells both. Anything else is refused before it is sent."""
"""`owner/name`, as GitHub spells both. Anything else is refused before it is sent."""

DEFAULT_WATCH_SECONDS = 3600.0
MAX_WATCH_SECONDS = 7 * 24 * 3600.0
MAX_FILES_PER_COMMIT = 20
MAX_FILE_CHARS = 200_000
LOG_LINES = 120


class ReposInputError(ValueError):
    """A call whose arguments the model can fix, said in a sentence it can act on."""


# ---------------------------------------------------------------------- handlers: reads


async def me(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    who = await client.me(run.ctx.profile)
    return {
        "login": who.login,
        "connected_with": "token" if who.kind == "pat" else "app",
        "reach": "all repositories" if who.selection == "all" else "chosen repositories",
        "repositories": who.repositories,
    }


async def find(run: RunContext[PackContext], client: ReposClient) -> list[dict[str, Any]]:
    page = await client.find(
        run.ctx.profile,
        query=str(run.input.get("query") or ""),
        owner=str(run.input.get("owner") or run.ctx.defaults.get("repos.owner") or ""),
        limit=limit(run.input.get("limit")),
    )
    _confess(run, page.notice)
    return [_repo(item) for item in page.items]


async def inspect(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    return _repo(await client.repo(run.ctx.profile, full_name(run.input)))


async def pulls(run: RunContext[PackContext], client: ReposClient) -> list[dict[str, Any]]:
    page = await client.pulls(
        run.ctx.profile,
        full_name(run.input),
        state=str(run.input.get("state") or "open"),
        limit=limit(run.input.get("limit")),
    )
    _confess(run, page.notice)
    return [_pull(item) for item in page.items]


async def read_pull(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    detail = await client.pull(run.ctx.profile, full_name(run.input), int(run.input["number"]))
    open_threads = [thread for thread in detail.threads if not thread.resolved]
    return {
        **_pull(detail.pull),
        "body": detail.body,
        "reviews": [
            {"author": review.author, "state": review.state, "body": review.body}
            for review in detail.reviews
        ],
        "open_threads": [
            {"path": t.path, "line": t.line, "author": t.author, "body": t.body}
            for t in open_threads
        ],
        "resolved_threads": len(detail.threads) - len(open_threads),
        "checks": [_check(check, detail.pull.repo) for check in detail.checks],
    }


async def issues(run: RunContext[PackContext], client: ReposClient) -> list[dict[str, Any]]:
    page = await client.issues(
        run.ctx.profile,
        full_name(run.input),
        state=str(run.input.get("state") or "open"),
        limit=limit(run.input.get("limit")),
    )
    _confess(run, page.notice)
    return [_issue(item) for item in page.items]


async def checks(run: RunContext[PackContext], client: ReposClient) -> list[dict[str, Any]]:
    repo = full_name(run.input)
    number = run.input.get("number")
    ref = str(run.input.get("ref") or "")
    if (number is None) == (not ref):
        message = "Give exactly one of `number` (a pull request) or `ref` (a branch or commit)."
        raise ReposInputError(message)
    summary, runs = await client.checks(
        run.ctx.profile, repo, f"pull/{number}" if number is not None else ref
    )
    run.notice(f"overall: {summary or 'none'}")
    return [_check(check, repo) for check in runs]


async def log(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    excerpt = await client.log(
        run.ctx.profile,
        full_name(run.input),
        str(run.input["run"]),
        starting_at=str(run.input.get("starting_at") or ""),
        lines=LOG_LINES,
    )
    if excerpt.truncated:
        run.notice(f"showing {excerpt.shown} of {excerpt.total} lines")
    return {"text": excerpt.text, "lines_shown": excerpt.shown, "lines_total": excerpt.total}


async def read(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    excerpt = await client.read(
        run.ctx.profile,
        full_name(run.input),
        str(run.input["path"]),
        ref=str(run.input.get("ref") or ""),
    )
    if excerpt.truncated:
        run.notice(f"showing {excerpt.shown} of {excerpt.total} characters")
    return {**dict(excerpt.extra), "text": excerpt.text}


async def tree(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    page = await client.tree(
        run.ctx.profile,
        full_name(run.input),
        str(run.input.get("path") or ""),
        ref=str(run.input.get("ref") or ""),
    )
    _confess(run, page.notice)
    return {"entries": list(page.items)}


# ---------------------------------------------------------------------- handlers: writes


async def create(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    created = await client.create(
        run.ctx.profile,
        name=str(run.input["name"]),
        owner=str(run.input.get("owner") or run.ctx.defaults.get("repos.owner") or ""),
        visibility=str(
            run.input.get("visibility") or run.ctx.defaults.get("repos.visibility") or "private"
        ),
        description=str(run.input.get("description") or ""),
    )
    return _repo(created)


async def set_visibility(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    changed = await client.update(
        run.ctx.profile, full_name(run.input), {"visibility": str(run.input["visibility"])}
    )
    return _repo(changed)


async def delete(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    repo = full_name(run.input)
    await client.delete(run.ctx.profile, repo)
    return {"deleted": repo}


async def branch(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    return await client.branch(
        run.ctx.profile,
        full_name(run.input),
        str(run.input["name"]),
        start=str(run.input.get("start") or ""),
    )


async def delete_branch(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    name = str(run.input["name"])
    await client.delete_branch(run.ctx.profile, full_name(run.input), name)
    return {"deleted_branch": name}


async def commit(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    files = list(run.input["files"])
    if len(files) > MAX_FILES_PER_COMMIT:
        message = (
            f"{len(files)} files is more than one commit here takes ({MAX_FILES_PER_COMMIT}); "
            "split them into several commits on the same branch."
        )
        raise ReposInputError(message)
    oversized = [f["path"] for f in files if len(str(f["content"])) > MAX_FILE_CHARS]
    if oversized:
        message = (
            f"{', '.join(oversized)} is over {MAX_FILE_CHARS:,} characters; a file that large is "
            "not written from a conversation."
        )
        raise ReposInputError(message)
    return await client.commit(
        run.ctx.profile,
        full_name(run.input),
        branch=str(run.input["branch"]),
        message=str(run.input["message"]),
        files=[{"path": str(f["path"]), "content": str(f["content"])} for f in files],
        base=str(run.input.get("base") or ""),
    )


async def open_pull(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    fields = {
        key: run.input[key]
        for key in ("title", "head", "base", "body", "draft")
        if run.input.get(key) is not None
    }
    return _pull(await client.open_pull(run.ctx.profile, full_name(run.input), fields))


async def update_pull(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    changes = {
        key: run.input[key]
        for key in ("title", "body", "draft", "state")
        if run.input.get(key) is not None
    }
    if not changes:
        message = "Say what to change: `title`, `body`, `draft` or `state`."
        raise ReposInputError(message)
    pull = await client.update_pull(
        run.ctx.profile, full_name(run.input), int(run.input["number"]), changes
    )
    return _pull(pull)


async def merge(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    return await client.merge(
        run.ctx.profile,
        full_name(run.input),
        int(run.input["number"]),
        method=str(run.input.get("method") or "squash"),
        delete_branch=bool(run.input.get("delete_branch", False)),
    )


async def review(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    event = str(run.input["event"])
    body = str(run.input.get("body") or "")
    if event != "approve" and not body:
        message = "A review that requests changes or comments needs a `body` saying why."
        raise ReposInputError(message)
    return await client.review(
        run.ctx.profile, full_name(run.input), int(run.input["number"]), event=event, body=body
    )


async def comment(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    return await client.comment(
        run.ctx.profile, full_name(run.input), int(run.input["number"]), str(run.input["body"])
    )


async def open_issue(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    issue = await client.open_issue(
        run.ctx.profile,
        full_name(run.input),
        title=str(run.input["title"]),
        body=str(run.input.get("body") or ""),
        labels=[str(label) for label in run.input.get("labels") or ()],
    )
    return _issue(issue)


async def close_issue(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    state = "open" if run.input.get("reopen") else "closed"
    issue = await client.set_issue_state(
        run.ctx.profile, full_name(run.input), int(run.input["number"]), state
    )
    return _issue(issue)


async def rerun(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    return await client.rerun(
        run.ctx.profile,
        full_name(run.input),
        str(run.input["run"]),
        failed_only=bool(run.input.get("failed_only", False)),
    )


async def cancel_run(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    return await client.cancel_run(run.ctx.profile, full_name(run.input), str(run.input["run"]))


async def dispatch(run: RunContext[PackContext], client: ReposClient) -> dict[str, Any]:
    return await client.dispatch(
        run.ctx.profile,
        full_name(run.input),
        str(run.input["workflow"]),
        ref=str(run.input["ref"]),
        inputs=dict(run.input.get("inputs") or {}),
    )


# ---------------------------------------------------------------------- shared


def repo_field(description: str) -> Any:
    return string_schema().regex(REPO_PATTERN).describe(description)


def full_name(raw: Mapping[str, Any]) -> str:
    """The `repo` argument, checked again where it is used: a call can arrive by any route."""
    repo = str(raw.get("repo") or "").strip()
    if not re.fullmatch(REPO_PATTERN, repo):
        message = f"`repo` must be owner/name, like octo/hello; got {repo!r}."
        raise ReposInputError(message)
    return repo


def limit(raw: object) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(1, min(raw, MAX_LIMIT))
    return DEFAULT_LIMIT


def watch_seconds(raw: object) -> float:
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        return min(float(raw), MAX_WATCH_SECONDS)
    return DEFAULT_WATCH_SECONDS


def watch_target_fits(until: str, target: Mapping[str, Any]) -> bool:
    if until == "checks_settled":
        return ("number" in target) != ("ref" in target)
    if until == "run_completed":
        return "run" in target
    return "number" in target


def _confess(run: RunContext[PackContext], notice: str) -> None:
    """Nothing truncates silently: a capped list says by how much."""
    if notice:
        run.notice(notice)


def _repo(repo: Repo) -> dict[str, Any]:
    return {
        "repo": repo.full_name,
        "private": repo.private,
        "default_branch": repo.default_branch,
        "description": repo.description,
        "open_pulls": repo.open_pulls,
        "open_issues": repo.open_issues,
        "ci": repo.ci,
        "url": repo.url,
    }


def _pull(pull: Pull) -> dict[str, Any]:
    return {
        "repo": pull.repo,
        "number": pull.number,
        "title": pull.title,
        "state": pull.state,
        "author": pull.author,
        "draft": pull.draft,
        "head": pull.head,
        "base": pull.base,
        "mergeable": pull.mergeable,
        "checks": pull.checks,
        "url": pull.url,
    }


def _issue(issue: Issue) -> dict[str, Any]:
    return {
        "repo": issue.repo,
        "number": issue.number,
        "title": issue.title,
        "state": issue.state,
        "author": issue.author,
        "labels": list(issue.labels),
        "url": issue.url,
    }


def _check(check: CheckRun, repo: str) -> dict[str, Any]:
    return {
        "repo": repo,
        "run": check.id,
        "name": check.name,
        "status": check.status,
        "conclusion": check.conclusion,
        "workflow": check.workflow,
        "failing_steps": list(check.failing_steps),
        "url": check.url,
    }


__all__ = [
    "DEFAULT_WATCH_SECONDS",
    "LOG_LINES",
    "MAX_FILES_PER_COMMIT",
    "MAX_FILE_CHARS",
    "MAX_WATCH_SECONDS",
    "REPO_PATTERN",
    "ReposInputError",
    "branch",
    "cancel_run",
    "checks",
    "close_issue",
    "comment",
    "commit",
    "create",
    "delete",
    "delete_branch",
    "dispatch",
    "find",
    "full_name",
    "issues",
    "limit",
    "log",
    "me",
    "merge",
    "open_issue",
    "open_pull",
    "pulls",
    "read",
    "read_pull",
    "repo_field",
    "rerun",
    "review",
    "set_visibility",
    "tree",
    "update_pull",
    "watch_seconds",
    "watch_target_fits",
]
