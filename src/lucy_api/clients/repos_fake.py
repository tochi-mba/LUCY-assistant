"""An in-memory repository host, so the `repos` pack's tests need no account and no network.

It satisfies `ReposClient` the way `FakeMusicClient` satisfies `MusicClient`: seeded state, a
flag for the case that has no natural representation in data (`is_connected`), and a record of
every call with the profile it was made under -- which credential set a call used is invisible
in its answer and is exactly what a multi-profile bug gets wrong. It lives beside the real
client rather than inside it only because both together would pass the file-length limit.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from lucy_api.clients.errors import AbsentError, ConflictError, NotConnectedError
from lucy_api.clients.repos import (
    SERVICE,
    ChangedFile,
    CheckRun,
    Excerpt,
    Identity,
    Issue,
    Page,
    Pull,
    PullDetail,
    Repo,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lucy_api.clients.repos import ReposClient


FILE = "file"
ISSUE = "issue"
LOG = "log"
PULL_REQUEST = "pull request"
REPOSITORY = "repository"
SUBSCRIPTION = "subscription"
NOT_CONNECTED = "connect repositories first"
TAKEN = "a repository of that name exists"


def _missing(what: str) -> AbsentError:
    return AbsentError(SERVICE, 404, f"no {what}", "not-found")


class FakeReposClient:
    """A host with repositories, pull requests, issues, checks and files, all in memory."""

    def __init__(self, login: str = "octo") -> None:
        self.is_connected = True
        self.identity = Identity(login=login, kind="app", selection="selected", repositories=0)
        self.repos: dict[str, Repo] = {}
        self.pull_requests: dict[tuple[str, int], PullDetail] = {}
        self.changed: dict[tuple[str, int], tuple[ChangedFile, ...]] = {}
        self.issue_list: dict[tuple[str, int], Issue] = {}
        self.check_runs: dict[tuple[str, str], tuple[str, tuple[CheckRun, ...]]] = {}
        self.logs: dict[tuple[str, str], str] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.refuse: dict[str, Exception] = {}
        """Method name to the error that method raises, to script a service refusing."""

    # ---------------------------------------------------------------- seeding

    def seed_repo(self, repo: Repo) -> None:
        self.repos[repo.full_name] = repo
        self.identity = replace(self.identity, repositories=len(self.repos))

    def seed_pull(self, detail: PullDetail) -> None:
        self.pull_requests[(detail.pull.repo, detail.pull.number)] = detail

    def seed_issue(self, issue: Issue) -> None:
        self.issue_list[(issue.repo, issue.number)] = issue

    def seed_checks(self, repo: str, ref: str, summary: str, runs: Sequence[CheckRun]) -> None:
        self.check_runs[(repo, ref)] = (summary, tuple(runs))

    # ---------------------------------------------------------------- recording

    def _call(self, method: str, profile: str, /, **arguments: Any) -> None:
        self.calls.append((method, profile, arguments))
        if not self.is_connected:
            raise NotConnectedError(SERVICE, 502, NOT_CONNECTED, "credential-missing")
        refused = self.refuse.get(method)
        if refused is not None:
            raise refused

    def _repo(self, full_name: str) -> Repo:
        found = self.repos.get(full_name)
        if found is None:
            raise _missing(REPOSITORY)
        return found

    # ---------------------------------------------------------------- the client

    async def me(self, profile: str) -> Identity:
        self._call("me", profile)
        return self.identity

    async def find(
        self, profile: str, *, query: str = "", owner: str = "", limit: int = 20
    ) -> Page[Repo]:
        self._call("find", profile, query=query, owner=owner, limit=limit)
        matched = tuple(
            repo
            for repo in self.repos.values()
            if query.lower() in repo.full_name.lower()
            and (not owner or repo.full_name.startswith(owner + "/"))
        )
        return Page(items=matched[:limit], total=len(matched))

    async def repo(self, profile: str, full_name: str) -> Repo:
        self._call("repo", profile, full_name=full_name)
        return self._repo(full_name)

    async def create(
        self, profile: str, *, name: str, owner: str, visibility: str, description: str
    ) -> Repo:
        self._call("create", profile, name=name, owner=owner, visibility=visibility)
        full_name = f"{owner or self.identity.login}/{name}"
        if full_name in self.repos:
            raise ConflictError(SERVICE, 409, TAKEN, "conflict")
        repo = Repo(
            full_name=full_name,
            private=visibility == "private",
            default_branch="main",
            description=description,
        )
        self.seed_repo(repo)
        return repo

    async def update(self, profile: str, full_name: str, changes: Mapping[str, Any]) -> Repo:
        self._call("update", profile, full_name=full_name, changes=dict(changes))
        repo = self._repo(full_name)
        if "visibility" in changes:
            repo = replace(repo, private=changes["visibility"] == "private")
        self.repos[full_name] = repo
        return repo

    async def delete(self, profile: str, full_name: str) -> None:
        self._call("delete", profile, full_name=full_name)
        self._repo(full_name)
        del self.repos[full_name]

    async def pulls(
        self, profile: str, full_name: str, *, state: str, limit: int = 20
    ) -> Page[Pull]:
        self._call("pulls", profile, full_name=full_name, state=state)
        found = tuple(
            detail.pull
            for (repo, _), detail in sorted(self.pull_requests.items())
            if repo == full_name and state in {"all", detail.pull.state}
        )
        return Page(items=found[:limit], total=len(found))

    async def pull(self, profile: str, full_name: str, number: int) -> PullDetail:
        self._call("pull", profile, full_name=full_name, number=number)
        detail = self.pull_requests.get((full_name, number))
        if detail is None:
            raise _missing(PULL_REQUEST)
        return detail

    async def changes(
        self, profile: str, full_name: str, number: int, *, limit: int = 20
    ) -> Page[ChangedFile]:
        self._call("changes", profile, full_name=full_name, number=number)
        if (full_name, number) not in self.pull_requests:
            raise _missing(PULL_REQUEST)
        found = self.changed.get((full_name, number), ())
        return Page(items=found[:limit], total=len(found))

    async def open_pull(self, profile: str, full_name: str, fields: Mapping[str, Any]) -> Pull:
        self._call("open_pull", profile, full_name=full_name, **dict(fields))
        self._repo(full_name)
        number = 1 + max((n for repo, n in self.pull_requests if repo == full_name), default=0)
        pull = Pull(
            repo=full_name,
            number=number,
            title=str(fields["title"]),
            state="open",
            author=self.identity.login,
            draft=bool(fields.get("draft")),
            head=str(fields["head"]),
            base=str(fields.get("base") or "main"),
            mergeable="unknown",
        )
        self.seed_pull(PullDetail(pull=pull, body=str(fields.get("body") or "")))
        return pull

    async def update_pull(
        self, profile: str, full_name: str, number: int, changes: Mapping[str, Any]
    ) -> Pull:
        self._call("update_pull", profile, full_name=full_name, number=number, **dict(changes))
        detail = await self.pull(profile, full_name, number)
        pull = replace(
            detail.pull, **{k: v for k, v in changes.items() if k in {"title", "state", "draft"}}
        )
        self.seed_pull(replace(detail, pull=pull))
        return pull

    async def merge(
        self, profile: str, full_name: str, number: int, *, method: str, delete_branch: bool
    ) -> dict[str, Any]:
        self._call(
            "merge",
            profile,
            full_name=full_name,
            number=number,
            method=method,
            delete_branch=delete_branch,
        )
        detail = await self.pull(profile, full_name, number)
        self.seed_pull(replace(detail, pull=replace(detail.pull, state="merged")))
        return {"merged": True, "sha": "abc1234", "message": "merged"}

    async def review(
        self, profile: str, full_name: str, number: int, *, event: str, body: str
    ) -> dict[str, Any]:
        self._call("review", profile, full_name=full_name, number=number, event=event, body=body)
        return {"state": event, "url": f"https://example.test/{full_name}/pull/{number}"}

    async def comment(self, profile: str, full_name: str, number: int, body: str) -> dict[str, Any]:
        self._call("comment", profile, full_name=full_name, number=number, body=body)
        return {"url": f"https://example.test/{full_name}/issues/{number}#comment"}

    async def issues(
        self, profile: str, full_name: str, *, state: str, limit: int = 20
    ) -> Page[Issue]:
        self._call("issues", profile, full_name=full_name, state=state)
        found = tuple(
            issue
            for (repo, _), issue in sorted(self.issue_list.items())
            if repo == full_name and state in {"all", issue.state}
        )
        return Page(items=found[:limit], total=len(found))

    async def open_issue(
        self, profile: str, full_name: str, *, title: str, body: str, labels: Sequence[str]
    ) -> Issue:
        self._call(
            "open_issue", profile, full_name=full_name, title=title, body=body, labels=list(labels)
        )
        self._repo(full_name)
        number = 1 + max((n for repo, n in self.issue_list if repo == full_name), default=0)
        issue = Issue(
            repo=full_name,
            number=number,
            title=title,
            state="open",
            author=self.identity.login,
            labels=tuple(labels),
        )
        self.seed_issue(issue)
        return issue

    async def set_issue_state(self, profile: str, full_name: str, number: int, state: str) -> Issue:
        self._call("set_issue_state", profile, full_name=full_name, number=number, state=state)
        issue = self.issue_list.get((full_name, number))
        if issue is None:
            raise _missing(ISSUE)
        issue = replace(issue, state=state)
        self.seed_issue(issue)
        return issue

    async def checks(
        self, profile: str, full_name: str, ref: str
    ) -> tuple[str, tuple[CheckRun, ...]]:
        self._call("checks", profile, full_name=full_name, ref=ref)
        return self.check_runs.get((full_name, ref), ("none", ()))

    async def log(
        self, profile: str, full_name: str, run_id: str, *, starting_at: str, lines: int
    ) -> Excerpt:
        self._call("log", profile, full_name=full_name, run_id=run_id, starting_at=starting_at)
        text = self.logs.get((full_name, run_id))
        if text is None:
            raise _missing(LOG)
        all_lines = text.splitlines()
        start = next(
            (i for i, line in enumerate(all_lines) if starting_at and starting_at in line), 0
        )
        shown = all_lines[start : start + lines]
        return Excerpt(
            text="\n".join(shown),
            shown=len(shown),
            total=len(all_lines),
            truncated=len(shown) < len(all_lines),
        )

    async def rerun(
        self, profile: str, full_name: str, run_id: str, *, failed_only: bool
    ) -> dict[str, Any]:
        self._call("rerun", profile, full_name=full_name, run_id=run_id, failed_only=failed_only)
        return {"queued": True}

    async def cancel_run(self, profile: str, full_name: str, run_id: str) -> dict[str, Any]:
        self._call("cancel_run", profile, full_name=full_name, run_id=run_id)
        return {"cancelled": True}

    async def dispatch(
        self, profile: str, full_name: str, workflow: str, *, ref: str, inputs: Mapping[str, str]
    ) -> dict[str, Any]:
        self._call(
            "dispatch",
            profile,
            full_name=full_name,
            workflow=workflow,
            ref=ref,
            inputs=dict(inputs),
        )
        return {"dispatched": True}

    async def read(self, profile: str, full_name: str, path: str, *, ref: str) -> Excerpt:
        self._call("read", profile, full_name=full_name, path=path, ref=ref)
        text = self.files.get((full_name, path))
        if text is None:
            raise _missing(FILE)
        return Excerpt(
            text=text,
            shown=len(text),
            total=len(text),
            extra={"path": path, "ref": ref or "main", "binary": False},
        )

    async def tree(
        self, profile: str, full_name: str, path: str, *, ref: str
    ) -> Page[dict[str, Any]]:
        self._call("tree", profile, full_name=full_name, path=path, ref=ref)
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries = tuple(
            {"path": name, "type": "file", "size": len(body)}
            for (repo, name), body in sorted(self.files.items())
            if repo == full_name and name.startswith(prefix)
        )
        return Page(items=entries, total=len(entries))

    async def commit(  # noqa: PLR0913 - the protocol's shape
        self,
        profile: str,
        full_name: str,
        *,
        branch: str,
        message: str,
        files: Sequence[Mapping[str, str]],
        base: str,
    ) -> dict[str, Any]:
        self._call(
            "commit",
            profile,
            full_name=full_name,
            branch=branch,
            message=message,
            paths=[f["path"] for f in files],
            base=base,
        )
        self._repo(full_name)
        for one in files:
            self.files[(full_name, one["path"])] = one["content"]
        return {"sha": "def5678", "branch": branch, "url": f"https://example.test/{full_name}"}

    async def branch(
        self, profile: str, full_name: str, name: str, *, start: str
    ) -> dict[str, Any]:
        self._call("branch", profile, full_name=full_name, name=name, start=start)
        return {"name": name, "sha": "fed9876"}

    async def delete_branch(self, profile: str, full_name: str, name: str) -> None:
        self._call("delete_branch", profile, full_name=full_name, name=name)

    async def subscribe(self, profile: str, full_name: str, fields: Mapping[str, Any]) -> str:
        self._call("subscribe", profile, full_name=full_name, **dict(fields))
        identifier = f"gh_sub_{len(self.subscriptions) + 1}"
        self.subscriptions[identifier] = {"repo": full_name, **dict(fields), "state": "running"}
        return identifier

    async def subscription(self, profile: str, subscription_id: str) -> dict[str, Any]:
        self._call("subscription", profile, subscription_id=subscription_id)
        found = self.subscriptions.get(subscription_id)
        if found is None:
            raise _missing(SUBSCRIPTION)
        return {
            "state": str(found["state"]),
            "summary": str(found.get("summary", "")),
            "facts": dict(found.get("facts", {})),
            "excerpt": str(found.get("excerpt", "")),
        }

    async def unsubscribe(self, profile: str, subscription_id: str) -> None:
        self._call("unsubscribe", profile, subscription_id=subscription_id)
        self.subscriptions.pop(subscription_id, None)


if TYPE_CHECKING:

    def _satisfies(fake: FakeReposClient) -> ReposClient:
        """Static proof that the fake satisfies the seam."""
        return fake


__all__ = ["FakeReposClient"]
