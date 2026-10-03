"""The `repos` capability: gated by connection, asked about by permission, and able to watch.

Plans run through `Capabilities.execute` against `FakeReposClient`, so what is pinned is what a
model and a person meet: which operations exist when, what each answers, which sentence a
refusal reads as, what is asked about and under which permission, and that a watch is a
subscription a sibling ends.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.auth.exchange import DelegationRefusedError
from lucy_api.clients.errors import (
    AbsentError,
    ConflictError,
    RateLimitedError,
    UnavailableError,
)
from lucy_api.clients.repos import (
    ChangedFile,
    CheckRun,
    Issue,
    Pull,
    PullDetail,
    Repo,
    Review,
    Thread,
)
from lucy_api.clients.repos_fake import FakeReposClient
from lucy_api.context.types import Trust
from lucy_api.net.signing import sign
from lucy_api.packs.base import State
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.packs.repos import NO_SUBSCRIPTIONS, NOT_CONNECTED, WATCH_KINDS, ReposPack
from lucy_api.packs.repos_calls import (
    DEFAULT_WATCH_SECONDS,
    MAX_FILE_CHARS,
    MAX_FILES_PER_COMMIT,
    MAX_WATCH_SECONDS,
    full_name,
)
from lucy_api.packs.service import Capabilities, installed_packs
from lucy_api.permissions.gate import Grant
from lucy_api.prompt.docs import capability_doc, read_capability_doc
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.settings.policy import TurnPolicy
from lucy_api.work import Kind
from lucy_api.work import State as WorkState
from lucy_api.work.registry import Registry
from lucy_api.work.subscriptions import REPORT_ONLY_ADVICE, Subscriptions, SubscriptionSeam

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore

HELLO = "octo/hello"


LEVEL = {"level": "2"}


def seeded() -> FakeReposClient:
    fake = FakeReposClient()
    fake.seed_repo(Repo(full_name=HELLO, private=True, default_branch="main", ci="failure"))
    fake.seed_repo(Repo(full_name="octo/other", default_branch="main"))
    pull = Pull(
        repo=HELLO,
        number=42,
        title="Add it",
        state="open",
        author="octo",
        head="feature",
        base="main",
        mergeable="clean",
        checks="failure",
    )
    fake.seed_pull(
        PullDetail(
            pull=pull,
            body="Why",
            reviews=(Review(author="rev", state="approved"),),
            threads=(
                Thread(path="a.py", line=3, author="rev", body="nit"),
                Thread(path="b.py", line=1, author="rev", body="done", resolved=True),
            ),
            checks=(CheckRun(id="991", name="tests", conclusion="failure"),),
        )
    )
    fake.seed_issue(Issue(repo=HELLO, number=7, title="Bug", state="open", labels=("bug",)))
    fake.seed_checks(
        HELLO,
        "pull/42",
        "failure",
        (
            CheckRun(
                id="991",
                name="tests",
                status="completed",
                conclusion="failure",
                failing_steps=("pytest",),
            ),
        ),
    )
    fake.logs[(HELLO, "991")] = "\n".join(f"line {n}" for n in range(300)) + "\nError: boom"
    fake.files[(HELLO, "src/a.py")] = "print('a')\n"
    return fake


def setup(fake: FakeReposClient, mode: str = "auto") -> tuple[Capabilities, Any]:
    capabilities = Capabilities((HelpPack(), ReposPack("http://repos.test", client=fake)))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="work", session_id="ses_a", permission_mode=mode)
    )
    context.policy = replace(context.policy, confirm_outward_actions=False)
    return capabilities, context


async def run(fake: FakeReposClient, *steps: dict[str, Any], mode: str = "auto") -> Any:
    capabilities, context = setup(fake, mode)
    await capabilities.probe(context)
    return await capabilities.execute({"steps": list(steps)}, context)


async def test_deleting_asks_even_in_auto_until_the_person_allows_it() -> None:
    fake = seeded()
    result = await run(fake, step("repos.delete", repo=HELLO))
    [issue] = result["issues"]
    assert (issue["code"], issue["permission"]) == ("permission_required", "repos.destroy")
    assert not any(call[0] == "delete" for call in fake.calls)


def step(op: str, **inputs: Any) -> dict[str, Any]:
    return {"id": op.split(".")[1].lower(), "op": op, "input": inputs}


# --------------------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------------------


async def test_unconnected_repositories_are_listed_with_no_tools() -> None:
    fake = seeded()
    fake.is_connected = False
    capabilities, context = setup(fake)

    catalogue = await capabilities.probe(context)

    repos = next(row for row in capabilities.listings(catalogue) if row["id"] == "repos")
    assert repos["state"] == "not_connected"
    assert repos["offer_setup"] is True
    assert not any(
        tool["name"].startswith("repos.")
        for tool in capabilities.tools(catalogue, "ses_a")["tools"]
    )


async def test_a_connection_names_the_account_and_how_far_it_reaches() -> None:
    fake = seeded()
    capabilities, context = setup(fake)
    catalogue = await capabilities.probe(context)
    bound = catalogue.get("repos")
    assert bound is not None
    assert bound.availability.state is State.ready
    assert bound.availability.detail == "GitHub as @octo, 2 chosen repositories, through the app"

    fake.identity = replace(fake.identity, kind="pat", selection="all")
    pack = ReposPack("http://repos.test", client=fake)
    availability = await pack.probe(context)
    assert availability.detail == "GitHub as @octo, all repositories, with a token"
    assert ("me", "work", {}) in fake.calls


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (NoBrokerError("none"), "cannot act for this person yet"),
        (DelegationRefusedError("no"), "cannot act for this person yet"),
        (UnavailableError("repos", 503, "down"), "repositories could not be reached"),
        (TransportError("reset", audience="github-api"), "repositories could not be reached"),
    ],
)
async def test_a_probe_that_cannot_ask_is_unavailable(error: Exception, detail: str) -> None:
    fake = seeded()
    fake.refuse["me"] = error
    _, context = setup(fake)
    availability = await ReposPack("http://repos.test", client=fake).probe(context)
    assert (availability.state, availability.detail) == (State.unavailable, detail)


async def test_every_operation_is_bound_when_connected() -> None:
    capabilities, context = setup(seeded())
    catalogue = await capabilities.probe(context)
    names = {tool["name"] for tool in capabilities.tools(catalogue, "ses_a")["tools"]}
    assert {
        "repos.me",
        "repos.find",
        "repos.inspect",
        "repos.pulls",
        "repos.pull",
        "repos.changes",
        "repos.issues",
        "repos.checks",
        "repos.log",
        "repos.read",
        "repos.tree",
        "repos.create",
        "repos.setVisibility",
        "repos.delete",
        "repos.branch",
        "repos.deleteBranch",
        "repos.commit",
        "repos.openPull",
        "repos.updatePull",
        "repos.merge",
        "repos.review",
        "repos.comment",
        "repos.openIssue",
        "repos.closeIssue",
        "repos.rerun",
        "repos.cancelRun",
        "repos.dispatch",
        "repos.watch",
    } <= names


def test_the_capability_has_a_page_setup_and_trust_rules() -> None:
    pack = ReposPack("http://repos.test")
    assert pack.docs == capability_doc("repos")
    assert "Never ask for a token" in read_capability_doc("repos")
    plan = pack.setup()
    assert plan is not None
    assert [s.kind for s in plan.steps] == ["oauth", "api_key"]
    assert "never paste a password or token" in plan.summary
    assert pack.result_trust("repos.me", {}) is Trust.observed
    assert pack.result_trust("repos.pull", {}) is Trust.untrusted


def test_it_is_installed_with_its_own_address_and_audience() -> None:
    [pack] = [
        p
        for p in installed_packs(repos_base_url="http://elsewhere:9/", repos_audience="other-api")
        if p.id == "repos"
    ]
    assert isinstance(pack, ReposPack)
    assert (pack.base_url, pack.audience) == ("http://elsewhere:9", "other-api")


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------


async def test_reads_answer_with_narrow_projections_on_the_sessions_profile() -> None:
    fake = seeded()
    result = await run(
        fake,
        step("repos.me"),
        step("repos.find", query="hello"),
        step("repos.pulls", repo=HELLO),
        step("repos.pull", repo=HELLO, number=42),
        step("repos.issues", repo=HELLO, state="all", limit=500),
        step("repos.read", repo=HELLO, path="src/a.py"),
        step("repos.tree", repo=HELLO, path="src"),
        step("repos.inspect", repo=HELLO),
    )

    assert result["issues"] is None, result
    me, found, pulls, pull, issues, read, tree, inspected = result["steps"]
    assert me["data"] == {
        "login": "octo",
        "connected_with": "app",
        "reach": "chosen repositories",
        "repositories": 2,
    }
    assert found["items"][0]["repo"] == HELLO
    assert found["items"][0]["private"] is True
    assert pulls["items"][0]["number"] == 42
    assert pull["data"]["open_threads"] == [
        {"path": "a.py", "line": 3, "author": "rev", "body": "nit"}
    ]
    assert pull["data"]["resolved_threads"] == 1
    assert pull["data"]["checks"][0]["run"] == "991"
    assert issues["items"][0]["labels"] == ["bug"]
    assert read["data"]["text"] == "print('a')\n"
    assert tree["data"]["entries"][0]["path"] == "src/a.py"
    assert (inspected["data"]["default_branch"], inspected["data"]["ci"]) == ("main", "failure")
    assert {profile for _, profile, _ in fake.calls} == {"work"}
    issue_call = next(call for call in fake.calls if call[0] == "issues")
    assert issue_call[2]["state"] == "all"


async def test_what_a_pull_request_changes_says_which_patches_were_cut() -> None:
    fake = seeded()
    fake.changed[(HELLO, 42)] = (
        ChangedFile(path="src/a.py", status="modified", additions=2, deletions=1, patch="+a"),
        ChangedFile(
            path="src/big.py",
            status="renamed",
            patch="+b",
            patch_truncated=True,
            previous_path="src/old.py",
        ),
    )
    result = await run(fake, step("repos.changes", repo=HELLO, number=42, limit=1))
    [changes] = result["steps"]
    assert changes["data"][0] == {
        "path": "src/a.py",
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "patch": "+a",
    }
    assert "showing 1 of 2" in changes["notices"]
    assert not any("cut short" in notice for notice in changes["notices"])

    every = await run(fake, step("repos.changes", repo=HELLO, number=42))
    assert every["steps"][0]["data"][1]["previous_path"] == "src/old.py"
    assert any("src/big.py" in notice for notice in every["steps"][0]["notices"])

    absent = await run(fake, step("repos.changes", repo=HELLO, number=9))
    assert "no pull request" in absent["steps"][0]["error"]


async def test_find_uses_the_persons_default_owner_when_none_is_named() -> None:
    fake = seeded()
    capabilities, context = setup(fake)
    context.defaults["repos.owner"] = "octo"
    await capabilities.probe(context)
    await capabilities.execute({"steps": [step("repos.find")]}, context)
    find = next(call for call in fake.calls if call[0] == "find")
    assert find[2]["owner"] == "octo"


async def test_a_capped_list_says_by_how_much() -> None:
    fake = seeded()
    for n in range(30):
        fake.seed_repo(Repo(full_name=f"octo/r{n:02d}"))
    result = await run(fake, step("repos.find", query="octo/r", limit=5))
    assert "showing 5 of 30" in result["steps"][0]["notices"]


async def test_checks_take_exactly_one_target_and_report_the_overall_state() -> None:
    fake = seeded()
    result = await run(fake, step("repos.checks", repo=HELLO, number=42))
    [checks] = result["steps"]
    assert checks["items"][0]["failing_steps"] == ["pytest"]
    assert "overall: failure" in checks["notices"]

    both = await run(fake, step("repos.checks", repo=HELLO, number=42, ref="main"))
    neither = await run(fake, step("repos.checks", repo=HELLO))
    for refused in (both, neither):
        assert "Give exactly one of `number`" in refused["steps"][0]["error"]

    by_ref = await run(fake, step("repos.checks", repo=HELLO, ref="main"))
    assert list(by_ref["steps"][0]["items"]) == []
    assert "overall: none" in by_ref["steps"][0]["notices"]


async def test_a_log_is_windowed_from_what_to_look_for_and_says_so() -> None:
    fake = seeded()
    result = await run(fake, step("repos.log", repo=HELLO, run="991", starting_at="Error"))
    [log] = result["steps"]
    assert log["data"]["text"] == "Error: boom"
    assert log["data"]["lines_total"] == 301
    assert "showing 1 of 301 lines" in log["notices"]

    fake.logs[(HELLO, "992")] = "ok"
    short = await run(fake, step("repos.log", repo=HELLO, run="992"))
    assert short["steps"][0]["data"]["lines_total"] == 1
    assert not any("showing" in notice for notice in short["steps"][0]["notices"])


async def test_a_large_file_read_confesses_the_cut() -> None:
    fake = seeded()

    async def cut(profile: str, full_name: str, path: str, *, ref: str) -> Any:
        from lucy_api.clients.repos import Excerpt

        return Excerpt(text="abc", shown=3, total=900, truncated=True, extra={"path": path})

    fake.read = cut  # type: ignore[method-assign]
    result = await run(fake, step("repos.read", repo=HELLO, path="big.txt"))
    assert "showing 3 of 900 characters" in result["steps"][0]["notices"]


# --------------------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------------------


async def test_writes_do_what_they_say_with_the_persons_defaults() -> None:
    fake = seeded()
    capabilities, context = setup(fake)
    context.defaults.update({"repos.owner": "acme", "repos.visibility": "internal"})
    context.grants["repos.destroy"] = Grant("repos.destroy", "allow", "work")
    await capabilities.probe(context)
    files = [{"path": "src/b.py", "content": "b"}]
    result = await capabilities.execute(
        {
            "steps": [
                step("repos.create", name="scratch"),
                step("repos.branch", repo=HELLO, name="feature-2"),
                step("repos.commit", repo=HELLO, branch="feature-2", message="m", files=files),
                step("repos.openPull", repo=HELLO, title="T", head="feature-2", draft=True),
                step("repos.updatePull", repo=HELLO, number=42, title="Better"),
                step("repos.review", repo=HELLO, number=42, event="approve"),
                step("repos.comment", repo=HELLO, number=42, body="Thanks"),
                step("repos.merge", repo=HELLO, number=42, delete_branch=True),
                step("repos.openIssue", repo=HELLO, title="New", labels=["bug"]),
                step("repos.closeIssue", repo=HELLO, number=7),
                step("repos.rerun", repo=HELLO, run="991", failed_only=True),
                step("repos.cancelRun", repo=HELLO, run="991"),
                step(
                    "repos.dispatch",
                    repo=HELLO,
                    workflow="ci.yml",
                    ref="main",
                    inputs={"level": "2"},
                ),
                step("repos.setVisibility", repo="octo/other", visibility="public"),
                step("repos.deleteBranch", repo=HELLO, name="feature-2"),
                step("repos.delete", repo="acme/scratch"),
            ]
        },
        context,
    )

    assert result["issues"] is None, result
    data = {s["id"]: s["data"] for s in result["steps"]}
    assert data["create"]["repo"] == "acme/scratch"
    assert next(c for c in fake.calls if c[0] == "create")[2]["visibility"] == "internal"
    assert data["commit"]["branch"] == "feature-2"
    assert fake.files[(HELLO, "src/b.py")] == "b"
    assert data["openpull"]["draft"] is True
    assert data["updatepull"]["title"] == "Better"
    assert data["merge"]["merged"] is True
    assert next(c for c in fake.calls if c[0] == "merge")[2]["method"] == "squash"
    assert data["openissue"]["labels"] == ["bug"]
    assert data["closeissue"]["state"] == "closed"
    assert data["rerun"] == {"queued": True}
    assert data["dispatch"] == {"dispatched": True}
    assert next(c for c in fake.calls if c[0] == "dispatch")[2]["inputs"] == {"level": "2"}
    assert data["setvisibility"]["private"] is False
    assert data["deletebranch"] == {"deleted_branch": "feature-2"}
    assert data["delete"] == {"deleted": "acme/scratch"}


async def test_a_new_repository_is_private_unless_someone_said_otherwise() -> None:
    fake = seeded()
    result = await run(fake, step("repos.create", name="quiet"))
    assert result["steps"][0]["data"]["private"] is True
    assert result["steps"][0]["data"]["repo"] == "octo/quiet"


async def test_reopening_an_issue_is_closing_it_backwards() -> None:
    fake = seeded()
    result = await run(fake, step("repos.closeIssue", repo=HELLO, number=7, reopen=True))
    assert result["steps"][0]["data"]["state"] == "open"


@pytest.mark.parametrize(
    ("bad", "says"),
    [
        (step("repos.updatePull", repo=HELLO, number=42), "Say what to change"),
        (step("repos.review", repo=HELLO, number=42, event="request_changes"), "needs a `body`"),
        (
            step(
                "repos.commit",
                repo=HELLO,
                branch="b",
                message="m",
                files=[{"path": f"f{n}", "content": "x"} for n in range(MAX_FILES_PER_COMMIT + 1)],
            ),
            "split them into several commits",
        ),
        (
            step(
                "repos.commit",
                repo=HELLO,
                branch="b",
                message="m",
                files=[{"path": "huge.txt", "content": "x" * (MAX_FILE_CHARS + 1)}],
            ),
            "huge.txt is over",
        ),
    ],
)
async def test_a_call_the_model_can_fix_is_refused_with_the_fix(
    bad: dict[str, Any], says: str
) -> None:
    result = await run(seeded(), bad)
    assert says in result["steps"][0]["error"]


def test_a_repository_name_is_checked_where_it_is_used_too() -> None:
    assert full_name({"repo": " octo/hello "}) == HELLO
    with pytest.raises(ValueError, match="must be owner/name"):
        full_name({"repo": "$found"})


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (AbsentError("repos", 404, "no pull request #9 on octo/hello"), "no pull request #9"),
        (ConflictError("repos", 409, "the branch has conflicts with main"), "has conflicts"),
        (RateLimitedError("repos", 429, "slow", retry_after=30.0), "Try again in 30s."),
        (RateLimitedError("repos", 429, "slow"), "limiting requests."),
    ],
)
async def test_a_refusal_reads_as_the_reason(error: Exception, says: str) -> None:
    fake = seeded()
    fake.refuse["merge"] = error
    result = await run(fake, step("repos.merge", repo=HELLO, number=42))
    assert says in result["steps"][0]["error"]


async def test_a_credential_that_lapsed_mid_conversation_reads_as_a_link_to_offer() -> None:
    fake = seeded()
    capabilities, context = setup(fake)
    await capabilities.probe(context)
    fake.is_connected = False
    result = await capabilities.execute({"steps": [step("repos.pulls", repo=HELLO)]}, context)
    assert NOT_CONNECTED in result["steps"][0]["error"]


# --------------------------------------------------------------------------------------
# What is asked about
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "inputs", "permission"),
    [
        ("repos.comment", {"repo": HELLO, "number": 1, "body": "x"}, "repos.comment"),
        (
            "repos.commit",
            {
                "repo": HELLO,
                "branch": "b",
                "message": "m",
                "files": [{"path": "a", "content": "b"}],
            },
            "repos.change",
        ),
        ("repos.merge", {"repo": HELLO, "number": 1}, "repos.merge"),
        ("repos.rerun", {"repo": HELLO, "run": "1"}, "repos.ci"),
        ("repos.create", {"name": "n"}, "repos.create"),
        ("repos.delete", {"repo": HELLO}, "repos.destroy"),
        (
            "repos.watch",
            {"repo": HELLO, "until": "pull_merged", "number": 1, "objective": "o"},
            "repos.watch",
        ),
    ],
)
async def test_each_kind_of_change_is_asked_under_its_own_permission(
    op: str, inputs: dict[str, Any], permission: str
) -> None:
    fake = seeded()
    result = await run(fake, {"id": "s", "op": op, "input": inputs}, mode="ask")
    [issue] = result["issues"]
    assert issue["code"] == "permission_required"
    assert permission in issue["message"] or issue.get("permission") == permission
    assert fake.calls[-1][0] == "me", "nothing ran before the person answered"


async def test_auto_still_asks_before_deleting_a_repository() -> None:
    fake = seeded()
    result = await run(fake, step("repos.delete", repo=HELLO), mode="auto")
    assert result["issues"][0]["code"] == "permission_required"
    assert HELLO in fake.repos


async def test_reads_never_ask() -> None:
    result = await run(seeded(), step("repos.pull", repo=HELLO, number=42), mode="ask")
    assert result["issues"] is None


def test_every_write_is_covered_by_a_permission_tallied_by_what_it_touches() -> None:
    pack = ReposPack("http://repos.test")
    covered = {name for p in pack.permissions() for name in p.covers}
    context = SimpleNamespace(policy=TurnPolicy())
    writes = {op.name for op in pack.operations(context) if op.effects == "write"}  # type: ignore[arg-type]
    assert writes == covered
    tallies = {p.id: p.tally for p in pack.permissions()}
    assert tallies.pop("repos.create") == "name"
    assert set(tallies.values()) == {"repo"}
    risks = {p.id: p.risk for p in pack.permissions()}
    assert risks["repos.destroy"] == "destructive"
    assert risks["repos.ci"] == "execute"


# --------------------------------------------------------------------------------------
# Watching
# --------------------------------------------------------------------------------------


async def watching(
    store: SessionStore,
    fake: FakeReposClient,
    *,
    policy: TurnPolicy | None = None,
    defaults: dict[str, object] | None = None,
    **inputs: Any,
) -> Any:
    created = await store.create("acct_a", CreateSession(), "watch")
    registry = Registry(now=lambda: datetime.now(UTC))
    subscriptions = Subscriptions(store, registry, signal_base_url="http://lucy.test/v1/signals")
    consent: list[float] = []

    async def record(lifetime: float) -> str:
        consent.append(lifetime)
        return "dgt_watch"

    capabilities, context = setup(fake)
    context.defaults.update(defaults or {})
    context.session_id = str(created["id"])
    context.subscriptions = SubscriptionSeam(
        subscriptions,
        account_id="acct_a",
        session_id=str(created["id"]),
        profile="work",
        consent=record,
    )
    if policy is not None:
        context.policy = policy
        context.subscriptions = context.subscriptions.under(policy)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "w", "op": "repos.watch", "input": inputs}]}, context
    )
    return result, registry, subscriptions, consent


async def test_a_watch_opens_a_subscription_the_sibling_holds_and_its_signal_ends(
    sessions_store: SessionStore,
) -> None:
    fake = seeded()
    result, registry, subscriptions, consent = await watching(
        sessions_store,
        fake,
        repo=HELLO,
        until="checks_settled",
        number=42,
        objective="Merge #42 once CI is green",
    )

    [watch] = result["steps"]
    assert watch["data"]["state"] == "running"
    assert watch["data"]["for_seconds"] == DEFAULT_WATCH_SECONDS
    assert consent == [DEFAULT_WATCH_SECONDS + 900]
    [(sibling_id, held)] = fake.subscriptions.items()
    assert held["kind"] == "checks_settled"
    assert held["target"] == {"number": 42}
    assert held["signal"]["url"].startswith("http://lucy.test/v1/signals/sub_")
    [row] = await subscriptions.open_rows()
    assert (row["sibling_id"], row["grant_id"], row["capability"]) == (
        sibling_id,
        "dgt_watch",
        "repos",
    )
    record = registry.running(row["session_id"])[0]
    assert record.kind is Kind.subscription

    body = json.dumps({"state": "fired", "summary": "CI is green"}).encode()
    await subscriptions.signal(str(row["id"]), sign(held["signal"]["secret"], body), body)
    await asyncio.sleep(0.05)
    assert registry.state_of(record.id) is WorkState.succeeded


async def test_a_quiet_watch_asks_no_consent(sessions_store: SessionStore) -> None:
    result, registry, _, consent = await watching(
        sessions_store,
        seeded(),
        repo=HELLO,
        until="run_completed",
        run="991",
        objective="Say when the run ends",
        wake=False,
        for_seconds=10 * MAX_WATCH_SECONDS,
    )
    assert result["steps"][0]["data"]["wake"] is False
    assert result["steps"][0]["data"]["for_seconds"] == MAX_WATCH_SECONDS
    assert consent == []
    await registry.shutdown()


async def test_a_watch_falls_to_the_persons_wake_default_and_says_so_in_its_schema(
    sessions_store: SessionStore,
) -> None:
    """New behaviour: with `wake_by_default` off, a watch the model did not ask to wake waits."""
    policy = TurnPolicy(wake_by_default=False)
    result, registry, _, consent = await watching(
        sessions_store,
        seeded(),
        policy=policy,
        repo=HELLO,
        until="pull_merged",
        number=1,
        objective="o",
    )
    assert result["steps"][0]["data"]["wake"] is False
    assert consent == []
    [watch] = [
        op
        for op in ReposPack("http://repos.test").operations(SimpleNamespace(policy=policy))
        if op.name == "repos.watch"
    ]  # type: ignore[arg-type]
    assert "(default false)" in watch.description
    await registry.shutdown()


async def test_a_watch_under_act_unattended_off_records_no_consent_and_tells_the_model(
    sessions_store: SessionStore,
) -> None:
    """New behaviour: the model is told at once not to promise to act while they are away."""
    result, registry, subscriptions, consent = await watching(
        sessions_store,
        seeded(),
        policy=TurnPolicy(act_unattended=False),
        repo=HELLO,
        until="checks_settled",
        number=42,
        objective="Merge #42 once CI is green",
    )
    data = result["steps"][0]["data"]
    assert data["wake"] is True
    assert data["advice"].endswith(REPORT_ONLY_ADVICE)
    assert consent == []
    [row] = await subscriptions.open_rows()
    assert row["grant_id"] is None
    await registry.shutdown()


@pytest.mark.parametrize(
    ("inputs", "says"),
    [
        ({"until": "checks_settled", "objective": "o"}, "`until: checks_settled` needs"),
        ({"until": "checks_settled", "number": 1, "ref": "m", "objective": "o"}, "needs"),
        ({"until": "run_completed", "number": 1, "objective": "o"}, "needs `run`"),
        ({"until": "pull_merged", "objective": "o"}, "needs `number`"),
        ({"until": "pull_merged", "number": 1, "objective": "  "}, "Say what the watch is for"),
    ],
)
async def test_a_watch_that_cannot_be_kept_says_what_it_needs(
    sessions_store: SessionStore, inputs: dict[str, Any], says: str
) -> None:
    result, registry, subscriptions, _ = await watching(
        sessions_store, seeded(), repo=HELLO, **inputs
    )
    assert says in result["steps"][0]["error"]
    assert await subscriptions.open_rows() == []
    await registry.shutdown()


async def test_a_watch_the_sibling_refuses_leaves_nothing_behind(
    sessions_store: SessionStore,
) -> None:
    fake = seeded()
    fake.refuse["subscribe"] = AbsentError("repos", 404, "no pull request #9 on octo/hello")
    result, registry, subscriptions, _ = await watching(
        sessions_store, fake, repo=HELLO, until="pull_merged", number=9, objective="o"
    )
    assert "no pull request #9" in result["steps"][0]["error"]
    await asyncio.sleep(0.05)
    assert await subscriptions.open_rows() == []
    await registry.shutdown()


async def test_without_a_subscription_store_a_watch_says_so() -> None:
    result = await run(
        seeded(), step("repos.watch", repo=HELLO, until="pull_merged", number=1, objective="o")
    )
    assert NO_SUBSCRIPTIONS in result["steps"][0]["error"]


def test_the_watch_kinds_are_the_four_the_sibling_knows() -> None:
    assert WATCH_KINDS == ("checks_settled", "pull_merged", "review_submitted", "run_completed")


async def test_release_and_check_go_to_the_sibling_on_the_rows_profile() -> None:
    fake = seeded()
    pack = ReposPack("http://repos.test", client=fake)
    sibling = await fake.subscribe("work", HELLO, {"kind": "pull_merged"})
    row = {"sibling_id": sibling, "profile": "work"}

    assert await pack.check_subscription(None, row) is None  # type: ignore[arg-type]
    fake.subscriptions[sibling].update(
        state="fired", summary="merged", facts={"sha": "abc", "nested": {"x": 1}}, excerpt="e"
    )
    signal = await pack.check_subscription(None, row)  # type: ignore[arg-type]
    assert signal is not None
    assert (signal.state, signal.summary, dict(signal.facts), signal.excerpt) == (
        "fired",
        "merged",
        {"sha": "abc"},
        "e",
    )
    fake.subscriptions[sibling].update(summary="")
    quiet = await pack.check_subscription(None, row)  # type: ignore[arg-type]
    assert quiet is not None
    assert quiet.summary == "fired"

    await pack.release_subscription(None, row)  # type: ignore[arg-type]
    assert sibling not in fake.subscriptions
    assert await pack.check_subscription(None, {"profile": "work"}) is None  # type: ignore[arg-type]
    await pack.release_subscription(None, {"profile": "work"})  # type: ignore[arg-type]
    assert [c[0] for c in fake.calls].count("unsubscribe") == 1


def test_without_an_override_the_hooks_reach_the_sibling_over_the_given_seam() -> None:
    from lucy_api.clients.repos import HttpReposClient
    from lucy_api.clients.testing import FakeHttp

    pack = ReposPack("http://repos.test/", audience="other-api")
    client = pack._client_on(FakeHttp())
    assert isinstance(client, HttpReposClient)
