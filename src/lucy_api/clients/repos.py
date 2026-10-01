"""Repositories, reduced to what a person would say about them.

This is the hub's side of the `repos` contract: the HTTP shape Github-api, the family's
implementation, answers to, and that any other implementation pointed at with
`LUCY_REPOS_API_BASE_URL` has to answer to, with tokens minted for its own audience. The
contract is capability-shaped -- `/v1/repos/{owner}/{name}/pulls`, `/checks`, `/commits` --
and never a provider's own routes, so a second provider changes the service and not the hub.

A GitHub pull request is one of the larger documents any API returns: users with avatars and
URLs for every relation, labels with colours, the head and base repositories in full, twice.
None of it helps a model say "#42 is green and mergeable". So every read is projected here to
a narrow dataclass and nothing else survives the boundary; a field the service starts sending
tomorrow cannot reach a prompt by accident.

The probe is `connected`: `GET /v1/me`, read only for whether the service refused it for a
missing credential (`502 credential-*`), which is what moves the capability to
`not_connected` and gets the person a link instead of an apology.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.errors import AbsentError
from lucy_api.clients.transport import Sibling, flag, given, nested, rows, segment, text
from lucy_api.clients.transport import number as whole

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lucy_api.packs.context import Http

SERVICE = "repos"
AUDIENCE = "github-api"
"""Github-api's own name. An implementation with another name is configured with its own
(`LUCY_REPOS_API_AUDIENCE`): keyring reads an audience as one service's name."""

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class Identity:
    """Who the connected account is, and how much of GitHub it can reach."""

    login: str
    kind: str = ""
    """`app` (an installation the person chose repositories for) or `pat` (a token they made)."""
    selection: str = ""
    """`all` or `selected`: whether the grant covers every repository or a chosen few."""
    repositories: int = 0


@dataclass(frozen=True, slots=True)
class Repo:
    """One repository, as a line a person would read."""

    full_name: str
    private: bool = False
    default_branch: str = ""
    description: str = ""
    open_pulls: int = 0
    open_issues: int = 0
    ci: str = ""
    """The latest state of the default branch's checks: success, failure, pending or none."""
    url: str = ""


@dataclass(frozen=True, slots=True)
class Pull:
    """One pull request: what it is, where it stands, and whether it can go in."""

    repo: str
    number: int
    title: str
    state: str = ""
    author: str = ""
    draft: bool = False
    head: str = ""
    base: str = ""
    mergeable: str = ""
    """`clean`, `blocked`, `dirty` (conflicts), `behind`, `unstable` or `unknown`."""
    checks: str = ""
    url: str = ""


@dataclass(frozen=True, slots=True)
class Review:
    author: str
    state: str
    body: str = ""


@dataclass(frozen=True, slots=True)
class Thread:
    """One open conversation on a line of a pull request."""

    path: str
    line: int
    author: str
    body: str
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One CI job, and for a failed one, which steps failed."""

    id: str
    name: str
    status: str = ""
    conclusion: str = ""
    workflow: str = ""
    failing_steps: tuple[str, ...] = ()
    url: str = ""


@dataclass(frozen=True, slots=True)
class PullDetail:
    """A pull request with what a reviewer reads before deciding."""

    pull: Pull
    body: str = ""
    reviews: tuple[Review, ...] = ()
    threads: tuple[Thread, ...] = ()
    checks: tuple[CheckRun, ...] = ()


@dataclass(frozen=True, slots=True)
class Issue:
    repo: str
    number: int
    title: str
    state: str = ""
    author: str = ""
    labels: tuple[str, ...] = ()
    url: str = ""


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Some of a list, and how many there were: a cap is always confessed."""

    items: tuple[T, ...]
    total: int

    @property
    def notice(self) -> str:
        """`showing 20 of 135`, or nothing when nothing was cut."""
        shown = len(self.items)
        return f"showing {shown} of {self.total}" if self.total > shown else ""


@dataclass(frozen=True, slots=True)
class Excerpt:
    """Part of a file or a log, and how much of it there was."""

    text: str
    shown: int = 0
    total: int = 0
    truncated: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)


class ReposClient(Protocol):
    """The repository operations Lucy binds. Every one is for one person and profile."""

    async def me(self, profile: str) -> Identity: ...
    async def find(
        self, profile: str, *, query: str = "", owner: str = "", limit: int = DEFAULT_LIMIT
    ) -> Page[Repo]: ...
    async def repo(self, profile: str, full_name: str) -> Repo: ...
    async def create(
        self, profile: str, *, name: str, owner: str, visibility: str, description: str
    ) -> Repo: ...
    async def update(self, profile: str, full_name: str, changes: Mapping[str, Any]) -> Repo: ...
    async def delete(self, profile: str, full_name: str) -> None: ...
    async def pulls(
        self, profile: str, full_name: str, *, state: str, limit: int = DEFAULT_LIMIT
    ) -> Page[Pull]: ...
    async def pull(self, profile: str, full_name: str, number: int) -> PullDetail: ...
    async def open_pull(self, profile: str, full_name: str, fields: Mapping[str, Any]) -> Pull: ...
    async def update_pull(
        self, profile: str, full_name: str, number: int, changes: Mapping[str, Any]
    ) -> Pull: ...
    async def merge(
        self, profile: str, full_name: str, number: int, *, method: str, delete_branch: bool
    ) -> dict[str, Any]: ...
    async def review(
        self, profile: str, full_name: str, number: int, *, event: str, body: str
    ) -> dict[str, Any]: ...
    async def comment(
        self, profile: str, full_name: str, number: int, body: str
    ) -> dict[str, Any]: ...
    async def issues(
        self, profile: str, full_name: str, *, state: str, limit: int = DEFAULT_LIMIT
    ) -> Page[Issue]: ...
    async def open_issue(
        self, profile: str, full_name: str, *, title: str, body: str, labels: Sequence[str]
    ) -> Issue: ...
    async def set_issue_state(
        self, profile: str, full_name: str, number: int, state: str
    ) -> Issue: ...
    async def checks(
        self, profile: str, full_name: str, ref: str
    ) -> tuple[str, tuple[CheckRun, ...]]: ...
    async def log(
        self, profile: str, full_name: str, run_id: str, *, starting_at: str, lines: int
    ) -> Excerpt: ...
    async def rerun(
        self, profile: str, full_name: str, run_id: str, *, failed_only: bool
    ) -> dict[str, Any]: ...
    async def cancel_run(self, profile: str, full_name: str, run_id: str) -> dict[str, Any]: ...
    async def dispatch(
        self, profile: str, full_name: str, workflow: str, *, ref: str, inputs: Mapping[str, str]
    ) -> dict[str, Any]: ...
    async def read(self, profile: str, full_name: str, path: str, *, ref: str) -> Excerpt: ...
    async def tree(
        self, profile: str, full_name: str, path: str, *, ref: str
    ) -> Page[dict[str, Any]]: ...
    async def commit(  # noqa: PLR0913 - one commit: where, on which branch, what, why, from what
        self,
        profile: str,
        full_name: str,
        *,
        branch: str,
        message: str,
        files: Sequence[Mapping[str, str]],
        base: str,
    ) -> dict[str, Any]: ...
    async def branch(
        self, profile: str, full_name: str, name: str, *, start: str
    ) -> dict[str, Any]: ...
    async def delete_branch(self, profile: str, full_name: str, name: str) -> None: ...
    async def subscribe(self, profile: str, full_name: str, fields: Mapping[str, Any]) -> str: ...
    async def subscription(self, profile: str, subscription_id: str) -> dict[str, Any]: ...
    async def unsubscribe(self, profile: str, subscription_id: str) -> None: ...


class HttpReposClient:
    """The real client. Every method answers with a projection, never a payload."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def me(self, profile: str) -> Identity:
        payload = await self._api.send("GET", "/v1/me", profile=profile)
        return Identity(
            login=text(payload, "login"),
            kind=text(payload, "kind"),
            selection=text(payload, "selection"),
            repositories=whole(payload, "repositories"),
        )

    async def find(
        self, profile: str, *, query: str = "", owner: str = "", limit: int = DEFAULT_LIMIT
    ) -> Page[Repo]:
        payload = await self._api.send(
            "GET",
            "/v1/repos",
            params=given(query=query or None, owner=owner or None, limit=_limit(limit)),
            profile=profile,
        )
        return _page(payload, "repos", _repo)

    async def repo(self, profile: str, full_name: str) -> Repo:
        return _repo(await self._api.send("GET", _path(full_name), profile=profile))

    async def create(
        self, profile: str, *, name: str, owner: str, visibility: str, description: str
    ) -> Repo:
        body = given(
            name=name, owner=owner or None, visibility=visibility, description=description or None
        )
        return _repo(await self._api.send("POST", "/v1/repos", body=body, profile=profile))

    async def update(self, profile: str, full_name: str, changes: Mapping[str, Any]) -> Repo:
        return _repo(
            await self._api.send("PATCH", _path(full_name), body=dict(changes), profile=profile)
        )

    async def delete(self, profile: str, full_name: str) -> None:
        await self._api.send("DELETE", _path(full_name), profile=profile)

    async def pulls(
        self, profile: str, full_name: str, *, state: str, limit: int = DEFAULT_LIMIT
    ) -> Page[Pull]:
        payload = await self._api.send(
            "GET",
            _path(full_name, "pulls"),
            params={"state": state, "limit": _limit(limit)},
            profile=profile,
        )
        return _page(payload, "pulls", _pull)

    async def pull(self, profile: str, full_name: str, number: int) -> PullDetail:
        payload = await self._api.send(
            "GET", _path(full_name, "pulls", str(number)), profile=profile
        )
        return PullDetail(
            pull=_pull(nested(payload, "pull")),
            body=text(payload, "body"),
            reviews=tuple(
                Review(author=text(row, "author"), state=text(row, "state"), body=text(row, "body"))
                for row in rows(payload, "reviews")
            ),
            threads=tuple(
                Thread(
                    path=text(row, "path"),
                    line=whole(row, "line"),
                    author=text(row, "author"),
                    body=text(row, "body"),
                    resolved=flag(row, "resolved"),
                )
                for row in rows(payload, "threads")
            ),
            checks=tuple(_check(row) for row in rows(payload, "checks")),
        )

    async def open_pull(self, profile: str, full_name: str, fields: Mapping[str, Any]) -> Pull:
        return _pull(
            await self._api.send(
                "POST", _path(full_name, "pulls"), body=dict(fields), profile=profile
            )
        )

    async def update_pull(
        self, profile: str, full_name: str, number: int, changes: Mapping[str, Any]
    ) -> Pull:
        return _pull(
            await self._api.send(
                "PATCH", _path(full_name, "pulls", str(number)), body=dict(changes), profile=profile
            )
        )

    async def merge(
        self, profile: str, full_name: str, number: int, *, method: str, delete_branch: bool
    ) -> dict[str, Any]:
        payload = await self._api.send(
            "POST",
            _path(full_name, "pulls", str(number), "merge"),
            body={"method": method, "delete_branch": delete_branch},
            profile=profile,
        )
        return {
            "merged": flag(payload, "merged"),
            "sha": text(payload, "sha"),
            "message": text(payload, "message"),
        }

    async def review(
        self, profile: str, full_name: str, number: int, *, event: str, body: str
    ) -> dict[str, Any]:
        payload = await self._api.send(
            "POST",
            _path(full_name, "pulls", str(number), "reviews"),
            body={"event": event, "body": body},
            profile=profile,
        )
        return {"state": text(payload, "state"), "url": text(payload, "url")}

    async def comment(self, profile: str, full_name: str, number: int, body: str) -> dict[str, Any]:
        payload = await self._api.send(
            "POST",
            _path(full_name, "issues", str(number), "comments"),
            body={"body": body},
            profile=profile,
        )
        return {"url": text(payload, "url")}

    async def issues(
        self, profile: str, full_name: str, *, state: str, limit: int = DEFAULT_LIMIT
    ) -> Page[Issue]:
        payload = await self._api.send(
            "GET",
            _path(full_name, "issues"),
            params={"state": state, "limit": _limit(limit)},
            profile=profile,
        )
        return _page(payload, "issues", _issue)

    async def open_issue(
        self, profile: str, full_name: str, *, title: str, body: str, labels: Sequence[str]
    ) -> Issue:
        return _issue(
            await self._api.send(
                "POST",
                _path(full_name, "issues"),
                body={"title": title, "body": body, "labels": list(labels)},
                profile=profile,
            )
        )

    async def set_issue_state(self, profile: str, full_name: str, number: int, state: str) -> Issue:
        return _issue(
            await self._api.send(
                "PATCH",
                _path(full_name, "issues", str(number)),
                body={"state": state},
                profile=profile,
            )
        )

    async def checks(
        self, profile: str, full_name: str, ref: str
    ) -> tuple[str, tuple[CheckRun, ...]]:
        payload = await self._api.send(
            "GET", _path(full_name, "checks"), params={"ref": ref}, profile=profile
        )
        return text(payload, "summary"), tuple(_check(row) for row in rows(payload, "checks"))

    async def log(
        self, profile: str, full_name: str, run_id: str, *, starting_at: str, lines: int
    ) -> Excerpt:
        payload = await self._api.send(
            "GET",
            _path(full_name, "checks", run_id, "log"),
            params=given(**{"from": starting_at or None}, lines=lines),
            profile=profile,
        )
        return _excerpt(payload)

    async def rerun(
        self, profile: str, full_name: str, run_id: str, *, failed_only: bool
    ) -> dict[str, Any]:
        payload = await self._api.send(
            "POST",
            _path(full_name, "checks", run_id, "rerun"),
            body={"failed_only": failed_only},
            profile=profile,
        )
        return {"queued": flag(payload, "queued")}

    async def cancel_run(self, profile: str, full_name: str, run_id: str) -> dict[str, Any]:
        payload = await self._api.send(
            "POST", _path(full_name, "runs", run_id, "cancel"), profile=profile
        )
        return {"cancelled": flag(payload, "cancelled")}

    async def dispatch(
        self, profile: str, full_name: str, workflow: str, *, ref: str, inputs: Mapping[str, str]
    ) -> dict[str, Any]:
        payload = await self._api.send(
            "POST",
            _path(full_name, "workflows", workflow, "dispatch"),
            body={"ref": ref, "inputs": dict(inputs)},
            profile=profile,
        )
        return {"dispatched": flag(payload, "dispatched")}

    async def read(self, profile: str, full_name: str, path: str, *, ref: str) -> Excerpt:
        payload = await self._api.send(
            "GET",
            _path(full_name, "contents"),
            params=given(path=path, ref=ref or None),
            profile=profile,
        )
        excerpt = _excerpt(payload)
        return Excerpt(
            text=excerpt.text,
            shown=excerpt.shown,
            total=excerpt.total,
            truncated=excerpt.truncated,
            extra={
                "path": text(payload, "path"),
                "ref": text(payload, "ref"),
                "binary": flag(payload, "binary"),
            },
        )

    async def tree(
        self, profile: str, full_name: str, path: str, *, ref: str
    ) -> Page[dict[str, Any]]:
        payload = await self._api.send(
            "GET",
            _path(full_name, "tree"),
            params=given(path=path or None, ref=ref or None),
            profile=profile,
        )
        return _page(
            payload,
            "entries",
            lambda row: {
                "path": text(row, "path"),
                "type": text(row, "type"),
                "size": whole(row, "size"),
            },
        )

    async def commit(  # noqa: PLR0913 - one commit: where, on which branch, what, why, from what
        self,
        profile: str,
        full_name: str,
        *,
        branch: str,
        message: str,
        files: Sequence[Mapping[str, str]],
        base: str,
    ) -> dict[str, Any]:
        payload = await self._api.send(
            "POST",
            _path(full_name, "commits"),
            body=given(
                branch=branch,
                message=message,
                files=[{"path": f["path"], "content": f["content"]} for f in files],
                base=base or None,
            ),
            profile=profile,
        )
        return {
            "sha": text(payload, "sha"),
            "branch": text(payload, "branch"),
            "url": text(payload, "url"),
        }

    async def branch(
        self, profile: str, full_name: str, name: str, *, start: str
    ) -> dict[str, Any]:
        payload = await self._api.send(
            "POST",
            _path(full_name, "branches"),
            body=given(name=name, start=start or None),
            profile=profile,
        )
        return {"name": text(payload, "name"), "sha": text(payload, "sha")}

    async def delete_branch(self, profile: str, full_name: str, name: str) -> None:
        await self._api.send("DELETE", _path(full_name, "branches", name), profile=profile)

    async def subscribe(self, profile: str, full_name: str, fields: Mapping[str, Any]) -> str:
        payload = await self._api.send(
            "POST", "/v1/subscriptions", body={"repo": full_name, **fields}, profile=profile
        )
        return text(payload, "id")

    async def subscription(self, profile: str, subscription_id: str) -> dict[str, Any]:
        payload = await self._api.send(
            "GET", f"/v1/subscriptions/{segment(subscription_id)}", profile=profile
        )
        return {
            "state": text(payload, "state"),
            "summary": text(payload, "summary"),
            "facts": nested(payload, "facts"),
            "excerpt": text(payload, "excerpt"),
        }

    async def unsubscribe(self, profile: str, subscription_id: str) -> None:
        try:
            await self._api.send(
                "DELETE", f"/v1/subscriptions/{segment(subscription_id)}", profile=profile
            )
        except AbsentError:
            # Already gone at the service -- expired or never accepted. Letting go of a thing
            # that is not there is letting go of it.
            return


def _path(full_name: str, *rest: str) -> str:
    owner, _, name = full_name.partition("/")
    tail = "".join(f"/{segment(part)}" for part in rest)
    return f"/v1/repos/{segment(owner)}/{segment(name)}{tail}"


def _limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _page[T](payload: Any, key: str, read: Any) -> Page[T]:
    items = tuple(read(row) for row in rows(payload, key))
    return Page(items=items, total=max(whole(payload, "total", len(items)), len(items)))


def _repo(payload: Any) -> Repo:
    return Repo(
        full_name=text(payload, "full_name"),
        private=flag(payload, "private"),
        default_branch=text(payload, "default_branch"),
        description=text(payload, "description"),
        open_pulls=whole(payload, "open_pulls"),
        open_issues=whole(payload, "open_issues"),
        ci=text(payload, "ci"),
        url=text(payload, "url"),
    )


def _pull(payload: Any) -> Pull:
    return Pull(
        repo=text(payload, "repo"),
        number=whole(payload, "number"),
        title=text(payload, "title"),
        state=text(payload, "state"),
        author=text(payload, "author"),
        draft=flag(payload, "draft"),
        head=text(payload, "head"),
        base=text(payload, "base"),
        mergeable=text(payload, "mergeable"),
        checks=text(payload, "checks"),
        url=text(payload, "url"),
    )


def _issue(payload: Any) -> Issue:
    return Issue(
        repo=text(payload, "repo"),
        number=whole(payload, "number"),
        title=text(payload, "title"),
        state=text(payload, "state"),
        author=text(payload, "author"),
        labels=tuple(str(label) for label in rows(payload, "labels")),
        url=text(payload, "url"),
    )


def _check(payload: Any) -> CheckRun:
    return CheckRun(
        id=text(payload, "id"),
        name=text(payload, "name"),
        status=text(payload, "status"),
        conclusion=text(payload, "conclusion"),
        workflow=text(payload, "workflow"),
        failing_steps=tuple(str(step) for step in rows(payload, "failing_steps")),
        url=text(payload, "url"),
    )


def _excerpt(payload: Any) -> Excerpt:
    return Excerpt(
        text=text(payload, "text"),
        shown=whole(payload, "shown"),
        total=whole(payload, "total"),
        truncated=flag(payload, "truncated"),
    )


__all__ = [
    "AUDIENCE",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SERVICE",
    "CheckRun",
    "Excerpt",
    "HttpReposClient",
    "Identity",
    "Issue",
    "Page",
    "Pull",
    "PullDetail",
    "Repo",
    "ReposClient",
    "Review",
    "Thread",
]
