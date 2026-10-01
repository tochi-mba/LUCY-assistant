"""The repos client: one audience, the profile on every call, and narrow projections.

Every method is driven over `FakeHttp`, so what is pinned is the wire -- the path, the verb,
the body, the audience, the profile header -- and what survives of each answer. A field the
service adds tomorrow must not reach a prompt, so projections are checked with payloads that
carry extra keys.
"""

from __future__ import annotations

import pytest

from lucy_api.clients.errors import (
    AbsentError,
    ConflictError,
    NotConnectedError,
    RateLimitedError,
)
from lucy_api.clients.repos import AUDIENCE, MAX_LIMIT, HttpReposClient, Page
from lucy_api.clients.testing import Answer, FakeHttp, problem
from lucy_api.clients.transport import PROFILE_HEADER

BASE = "http://repos.test"
REPO = "octo/hello world"
"""A name with a space, so every path shows it is escaped."""

PULL = {
    "repo": "octo/hello",
    "number": 42,
    "title": "Add the thing",
    "state": "open",
    "author": "octo",
    "draft": False,
    "head": "feature",
    "base": "main",
    "mergeable": "clean",
    "checks": "success",
    "url": "https://example.test/octo/hello/pull/42",
    "user": {"avatar_url": "https://example.test/a.png", "site_admin": False},
}
REPO_ROW = {
    "full_name": "octo/hello",
    "private": True,
    "default_branch": "main",
    "description": "A repository",
    "open_pulls": 2,
    "open_issues": 3,
    "ci": "failure",
    "url": "https://example.test/octo/hello",
    "node_id": "R_kgDO",
}
CHECK = {
    "id": "991",
    "name": "tests",
    "status": "completed",
    "conclusion": "failure",
    "workflow": "CI",
    "failing_steps": ["pytest"],
    "url": "https://example.test/run/991",
    "runner_id": 7,
}


def client(*answers: Answer) -> tuple[HttpReposClient, FakeHttp]:
    http = FakeHttp(*answers)
    return HttpReposClient(http, BASE + "/"), http


def assert_call(http: FakeHttp, method: str, path: str, *, profile: str = "work") -> None:
    call = http.last
    assert (call.method, call.url) == (method, BASE + path)
    assert call.audience == AUDIENCE
    assert call.headers == {PROFILE_HEADER: profile}


async def test_me_reads_who_and_how_much() -> None:
    repos, http = client(
        Answer(
            body={
                "login": "octo",
                "kind": "app",
                "selection": "selected",
                "repositories": 4,
                "token": "never-read",
            }
        )
    )
    identity = await repos.me("work")
    assert (identity.login, identity.kind, identity.selection, identity.repositories) == (
        "octo",
        "app",
        "selected",
        4,
    )
    assert_call(http, "GET", "/v1/me")


async def test_find_sends_only_what_was_given_and_confesses_the_cap() -> None:
    repos, http = client(Answer(body={"repos": [REPO_ROW], "total": 135}))
    page = await repos.find("work", query="hel", limit=10_000)
    assert http.last.params == {"query": "hel", "limit": MAX_LIMIT}
    assert page.notice == "showing 1 of 135"
    [repo] = page.items
    assert (repo.full_name, repo.private, repo.ci, repo.open_pulls) == (
        "octo/hello",
        True,
        "failure",
        2,
    )

    repos, http = client(Answer(body={"repos": []}))
    empty = await repos.find("work", owner="octo", limit=0)
    assert http.last.params == {"owner": "octo", "limit": 1}
    assert empty.notice == ""


def test_a_page_never_reports_fewer_than_it_holds() -> None:
    assert Page(items=(1, 2), total=1).notice == ""


async def test_repository_calls_escape_the_path_and_send_the_body() -> None:
    repos, http = client(
        Answer(body=REPO_ROW),
        Answer(body=REPO_ROW),
        Answer(body=REPO_ROW),
        Answer(status_code=204),
    )
    await repos.repo("work", REPO)
    assert_call(http, "GET", "/v1/repos/octo/hello%20world")
    await repos.create("work", name="new", owner="", visibility="private", description="")
    assert_call(http, "POST", "/v1/repos")
    assert http.last.json == {"name": "new", "visibility": "private"}
    await repos.update("work", REPO, {"visibility": "public"})
    assert_call(http, "PATCH", "/v1/repos/octo/hello%20world")
    assert http.last.json == {"visibility": "public"}
    assert await repos.delete("work", REPO) is None
    assert_call(http, "DELETE", "/v1/repos/octo/hello%20world")


async def test_a_new_repository_in_an_organisation_names_it() -> None:
    repos, http = client(Answer(body=REPO_ROW))
    await repos.create("work", name="new", owner="acme", visibility="public", description="d")
    assert http.last.json == {
        "name": "new",
        "owner": "acme",
        "visibility": "public",
        "description": "d",
    }


async def test_pull_requests_project_to_what_a_person_says() -> None:
    repos, http = client(
        Answer(body={"pulls": [PULL], "total": 1}),
        Answer(
            body={
                "pull": PULL,
                "body": "Why",
                "reviews": [{"author": "rev", "state": "approved", "body": "ok", "id": 1}],
                "threads": [
                    {"path": "a.py", "line": 3, "author": "rev", "body": "nit", "resolved": False}
                ],
                "checks": [CHECK],
            }
        ),
        Answer(body=PULL),
        Answer(body=PULL),
    )
    page = await repos.pulls("work", REPO, state="open", limit=5)
    assert http.last.params == {"state": "open", "limit": 5}
    assert page.items[0].mergeable == "clean"
    assert not hasattr(page.items[0], "user")

    detail = await repos.pull("work", REPO, 42)
    assert_call(http, "GET", "/v1/repos/octo/hello%20world/pulls/42")
    assert detail.body == "Why"
    assert detail.reviews[0].state == "approved"
    assert detail.threads[0].line == 3
    assert detail.checks[0].failing_steps == ("pytest",)

    await repos.open_pull("work", REPO, {"title": "t", "head": "f"})
    assert_call(http, "POST", "/v1/repos/octo/hello%20world/pulls")
    await repos.update_pull("work", REPO, 42, {"state": "closed"})
    assert_call(http, "PATCH", "/v1/repos/octo/hello%20world/pulls/42")
    assert http.last.json == {"state": "closed"}


async def test_merge_review_and_comment_say_what_happened() -> None:
    repos, http = client(
        Answer(body={"merged": True, "sha": "abc", "message": "done", "extra": 1}),
        Answer(body={"state": "approved", "url": "u", "id": 9}),
        Answer(body={"url": "c", "id": 9}),
    )
    merged = await repos.merge("work", REPO, 42, method="squash", delete_branch=True)
    assert http.last.json == {"method": "squash", "delete_branch": True}
    assert merged == {"merged": True, "sha": "abc", "message": "done"}
    reviewed = await repos.review("work", REPO, 42, event="approve", body="")
    assert_call(http, "POST", "/v1/repos/octo/hello%20world/pulls/42/reviews")
    assert reviewed == {"state": "approved", "url": "u"}
    commented = await repos.comment("work", REPO, 42, "Thanks")
    assert_call(http, "POST", "/v1/repos/octo/hello%20world/issues/42/comments")
    assert commented == {"url": "c"}


async def test_issues_list_open_and_change_state() -> None:
    issue = {
        "repo": "octo/hello",
        "number": 7,
        "title": "Bug",
        "state": "open",
        "author": "octo",
        "labels": ["bug"],
        "url": "u",
        "reactions": {},
    }
    repos, http = client(
        Answer(body={"issues": [issue], "total": 1}),
        Answer(body=issue),
        Answer(body={**issue, "state": "closed"}),
    )
    page = await repos.issues("work", REPO, state="all")
    assert page.items[0].labels == ("bug",)
    opened = await repos.open_issue("work", REPO, title="Bug", body="b", labels=["bug"])
    assert http.last.json == {"title": "Bug", "body": "b", "labels": ["bug"]}
    assert opened.number == 7
    closed = await repos.set_issue_state("work", REPO, 7, "closed")
    assert_call(http, "PATCH", "/v1/repos/octo/hello%20world/issues/7")
    assert closed.state == "closed"


async def test_ci_reads_and_controls() -> None:
    repos, http = client(
        Answer(body={"summary": "failure", "checks": [CHECK]}),
        Answer(body={"text": "line", "shown": 1, "total": 9, "truncated": True}),
        Answer(body={"queued": True}),
        Answer(body={"cancelled": True}),
        Answer(body={"dispatched": True}),
        Answer(body={"text": "x", "shown": 1, "total": 1}),
    )
    summary, runs = await repos.checks("work", REPO, "pull/42")
    assert http.last.params == {"ref": "pull/42"}
    assert (summary, runs[0].conclusion) == ("failure", "failure")

    excerpt = await repos.log("work", REPO, "991", starting_at="Error", lines=120)
    assert_call(http, "GET", "/v1/repos/octo/hello%20world/checks/991/log")
    assert http.last.params == {"from": "Error", "lines": 120}
    assert (excerpt.shown, excerpt.total, excerpt.truncated) == (1, 9, True)

    assert await repos.rerun("work", REPO, "991", failed_only=True) == {"queued": True}
    assert http.last.json == {"failed_only": True}
    assert await repos.cancel_run("work", REPO, "991") == {"cancelled": True}
    assert_call(http, "POST", "/v1/repos/octo/hello%20world/runs/991/cancel")
    assert await repos.dispatch("work", REPO, "ci.yml", ref="main", inputs={"a": "b"}) == {
        "dispatched": True
    }
    assert http.last.json == {"ref": "main", "inputs": {"a": "b"}}

    await repos.log("work", REPO, "991", starting_at="", lines=50)
    assert http.last.params == {"lines": 50}


async def test_contents_tree_commit_and_branches() -> None:
    repos, http = client(
        Answer(
            body={
                "path": "a.py",
                "ref": "main",
                "text": "x",
                "shown": 1,
                "total": 1,
                "binary": False,
                "sha": "s",
            }
        ),
        Answer(
            body={"entries": [{"path": "a.py", "type": "file", "size": 1, "sha": "s"}], "total": 1}
        ),
        Answer(body={"sha": "c", "branch": "f", "url": "u", "tree": {}}),
        Answer(body={"name": "f", "sha": "s"}),
        Answer(status_code=204),
    )
    read = await repos.read("work", REPO, "a.py", ref="")
    assert http.last.params == {"path": "a.py"}
    assert read.extra == {"path": "a.py", "ref": "main", "binary": False}
    tree = await repos.tree("work", REPO, "", ref="main")
    assert http.last.params == {"ref": "main"}
    assert tree.items == ({"path": "a.py", "type": "file", "size": 1},)
    committed = await repos.commit(
        "work",
        REPO,
        branch="f",
        message="m",
        files=[{"path": "a.py", "content": "y", "mode": "100644"}],
        base="",
    )
    assert http.last.json == {
        "branch": "f",
        "message": "m",
        "files": [{"path": "a.py", "content": "y"}],
    }
    assert committed == {"sha": "c", "branch": "f", "url": "u"}
    assert await repos.branch("work", REPO, "f", start="main") == {"name": "f", "sha": "s"}
    assert http.last.json == {"name": "f", "start": "main"}
    assert await repos.delete_branch("work", REPO, "f/x") is None
    assert_call(http, "DELETE", "/v1/repos/octo/hello%20world/branches/f%2Fx")


async def test_subscriptions_open_read_and_release() -> None:
    repos, http = client(
        Answer(status_code=201, body={"id": "gh_1", "state": "running"}),
        Answer(body={"state": "fired", "summary": "green", "facts": {"c": 1}, "excerpt": "e"}),
        Answer(status_code=204),
        problem(404, code="not-found"),
    )
    assert await repos.subscribe("work", "octo/hello", {"kind": "checks_settled"}) == "gh_1"
    assert http.last.json == {"repo": "octo/hello", "kind": "checks_settled"}
    assert await repos.subscription("work", "gh/1") == {
        "state": "fired",
        "summary": "green",
        "facts": {"c": 1},
        "excerpt": "e",
    }
    assert_call(http, "GET", "/v1/subscriptions/gh%2F1")
    assert await repos.unsubscribe("work", "gh_1") is None
    # Already gone at the service: letting go of what is not there is letting go of it.
    assert await repos.unsubscribe("work", "gh_1") is None


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (problem(404, code="not-found"), AbsentError),
        (problem(409, code="conflict", detail="merge conflict"), ConflictError),
        (problem(429, code="rate-limited", **{"Retry-After": "30"}), RateLimitedError),
        (problem(502, code="credential-missing"), NotConnectedError),
    ],
)
async def test_refusals_arrive_in_the_family_vocabulary(answer: Answer, error: type) -> None:
    repos, _ = client(answer)
    with pytest.raises(error):
        await repos.pull("work", REPO, 1)
