"""How a person merges, opens pull requests and watches them is what `repos` does unasked.

The bug, named: every merge squashed and kept its branch, every pull request opened ready for
review, and every watch lapsed after an hour, whatever the person's `github` settings said.
A model that left `method`, `delete_branch`, `draft` or `for_seconds` out got the service's
answer rather than the person's. A field the model did name still wins.

The merge method is the one that refuses rather than falls back: a method that cannot be read
is never guessed, so a merge that names none is refused with the fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from settings_client import SettingsRefused
from test_repos_pack import HELLO, seeded, setup, step, watching

from lucy_api.packs.repos_calls import (
    BRANCH_DELETED_BY_SETTING,
    DEFAULT_WATCH_SECONDS,
    MAX_WATCH_SECONDS,
    METHOD_NOT_KNOWN,
    watch_seconds,
)
from lucy_api.settings.defaults import (
    MAX_WATCH_HOURS,
    MEMORY_NAMESPACE,
    METHOD_UNKNOWN,
    REPOS_NAMESPACE,
    pack_defaults,
)

if TYPE_CHECKING:
    from lucy_api.clients.repos_fake import FakeReposClient
    from lucy_api.sessions.sql_store import SessionStore


class _Refusing(dict[str, Any]):
    """A resolved namespace in an outage: one key cannot be read and must not be guessed."""

    def __init__(self, refused: str, **values: Any) -> None:
        super().__init__(values)
        self._refused = refused

    def get(self, key: str, default: Any = None) -> Any:
        if key == self._refused:
            raise SettingsRefused("github", key)
        return super().get(key, default)


# --------------------------------------------------------------------------------------
# What the namespace supplies
# --------------------------------------------------------------------------------------


def test_each_habit_becomes_a_repos_default() -> None:
    chosen = {
        "merge_method": "rebase",
        "draft_pull_requests": True,
        "delete_branch_after_merge": True,
        "watch_default_hours": 24,
    }

    assert pack_defaults({REPOS_NAMESPACE: chosen}) == {
        "repos.merge_method": "rebase",
        "repos.draft": True,
        "repos.delete_branch": True,
        "repos.watch_seconds": 86_400.0,
    }


def test_the_catalogue_defaults_change_nothing_a_merge_or_a_pull_request_does() -> None:
    defaults = pack_defaults(
        {
            REPOS_NAMESPACE: {
                "merge_method": "squash",
                "draft_pull_requests": False,
                "delete_branch_after_merge": False,
                "watch_default_hours": 1,
            }
        }
    )

    assert defaults["repos.merge_method"] == "squash"
    assert (defaults["repos.draft"], defaults["repos.delete_branch"]) == (False, False)
    assert defaults["repos.watch_seconds"] == DEFAULT_WATCH_SECONDS


@pytest.mark.parametrize(("hours", "seconds"), [(0, 3_600.0), (-2, 3_600.0), (500, 604_800.0)])
def test_a_watch_length_is_held_between_an_hour_and_a_week(hours: int, seconds: float) -> None:
    defaults = pack_defaults({REPOS_NAMESPACE: {"watch_default_hours": hours}})

    assert defaults["repos.watch_seconds"] == seconds
    assert MAX_WATCH_HOURS * 3_600.0 == MAX_WATCH_SECONDS


def test_a_habit_of_the_wrong_kind_supplies_nothing() -> None:
    wrong = {
        "draft_pull_requests": "yes",
        "delete_branch_after_merge": 1,
        "watch_default_hours": True,
    }

    assert pack_defaults({REPOS_NAMESPACE: wrong}) == {}


@pytest.mark.parametrize("method", ["fast-forward", 3])
def test_a_merge_method_that_is_not_one_of_the_three_is_unknown(method: object) -> None:
    defaults = pack_defaults({REPOS_NAMESPACE: {"merge_method": method}})

    assert defaults == {"repos.merge_method": METHOD_UNKNOWN}


def test_a_merge_method_that_cannot_be_read_is_unknown_and_never_guessed() -> None:
    refused = _Refusing("merge_method", default_owner="acme", draft_pull_requests=True)

    assert pack_defaults({REPOS_NAMESPACE: refused}) == {
        "repos.owner": "acme",
        "repos.merge_method": METHOD_UNKNOWN,
        "repos.draft": True,
    }


def test_an_unreadable_namespace_marks_only_its_refusing_settings_unknown() -> None:
    """The bug, named: an outage would have squashed a merge the person wanted rebased."""
    assert pack_defaults({REPOS_NAMESPACE: None, MEMORY_NAMESPACE: None}) == {
        "repos.merge_method": METHOD_UNKNOWN,
        "notes.trust_floor": "unknown",
    }


# --------------------------------------------------------------------------------------
# What a call that does not say does
# --------------------------------------------------------------------------------------


async def acting(
    defaults: dict[str, object], *steps: dict[str, Any]
) -> tuple[Any, FakeReposClient]:
    fake = seeded()
    capabilities, context = setup(fake)
    context.defaults.update(defaults)
    await capabilities.probe(context)
    return await capabilities.execute({"steps": list(steps)}, context), fake


def merged_with(fake: FakeReposClient) -> dict[str, Any]:
    return next(call for call in fake.calls if call[0] == "merge")[2]


async def test_a_merge_that_names_nothing_lands_the_persons_way() -> None:
    result, fake = await acting(
        {"repos.merge_method": "rebase", "repos.delete_branch": True},
        step("repos.merge", repo=HELLO, number=42),
    )

    assert result["issues"] is None, result
    assert (merged_with(fake)["method"], merged_with(fake)["delete_branch"]) == ("rebase", True)
    assert BRANCH_DELETED_BY_SETTING in result["steps"][0]["notices"]


async def test_a_merge_that_names_its_method_and_branch_wins_over_the_settings() -> None:
    result, fake = await acting(
        {"repos.merge_method": "rebase", "repos.delete_branch": True},
        step("repos.merge", repo=HELLO, number=42, method="merge", delete_branch=False),
    )

    assert (merged_with(fake)["method"], merged_with(fake)["delete_branch"]) == ("merge", False)
    assert BRANCH_DELETED_BY_SETTING not in result["steps"][0]["notices"]


async def test_with_nothing_chosen_a_merge_still_squashes_and_keeps_the_branch() -> None:
    _, fake = await acting({}, step("repos.merge", repo=HELLO, number=42))

    assert (merged_with(fake)["method"], merged_with(fake)["delete_branch"]) == ("squash", False)


async def test_an_unknown_method_refuses_a_merge_that_names_none() -> None:
    result, fake = await acting(
        {"repos.merge_method": METHOD_UNKNOWN}, step("repos.merge", repo=HELLO, number=42)
    )

    assert result["steps"][0]["error"].endswith(METHOD_NOT_KNOWN)
    assert not any(call[0] == "merge" for call in fake.calls)


async def test_an_unknown_method_still_merges_when_the_call_names_one() -> None:
    _, fake = await acting(
        {"repos.merge_method": METHOD_UNKNOWN},
        step("repos.merge", repo=HELLO, number=42, method="rebase"),
    )

    assert merged_with(fake)["method"] == "rebase"


async def test_a_pull_request_opens_as_a_draft_when_the_person_always_drafts() -> None:
    result, _ = await acting(
        {"repos.draft": True},
        step("repos.openPull", repo=HELLO, title="T", head="feature"),
        {**step("repos.openPull", repo=HELLO, title="U", head="other", draft=False), "id": "ready"},
    )

    data = {s["id"]: s["data"] for s in result["steps"]}
    assert (data["openpull"]["draft"], data["ready"]["draft"]) == (True, False)


async def test_with_nothing_chosen_a_pull_request_sends_no_draft_field() -> None:
    _, fake = await acting({}, step("repos.openPull", repo=HELLO, title="T", head="feature"))

    assert "draft" not in next(call for call in fake.calls if call[0] == "open_pull")[2]


async def test_a_watch_that_names_no_length_lasts_as_long_as_the_person_chose(
    sessions_store: SessionStore,
) -> None:
    result, registry, _, _ = await watching(
        sessions_store,
        seeded(),
        {"repos.watch_seconds": 86_400.0},
        repo=HELLO,
        until="pull_merged",
        number=42,
        objective="Say when #42 merges",
        wake=False,
    )

    assert result["steps"][0]["data"]["for_seconds"] == 86_400.0
    await registry.shutdown()


async def test_a_watch_that_names_its_length_wins_over_the_setting(
    sessions_store: SessionStore,
) -> None:
    result, registry, _, _ = await watching(
        sessions_store,
        seeded(),
        {"repos.watch_seconds": 86_400.0},
        repo=HELLO,
        until="pull_merged",
        number=42,
        objective="Say when #42 merges",
        wake=False,
        for_seconds=60,
    )

    assert result["steps"][0]["data"]["for_seconds"] == 60.0
    await registry.shutdown()


@pytest.mark.parametrize(
    ("raw", "default", "seconds"),
    [
        (None, 7_200.0, 7_200.0),
        (None, None, DEFAULT_WATCH_SECONDS),
        (None, "a day", DEFAULT_WATCH_SECONDS),
        (None, 10 * MAX_WATCH_SECONDS, MAX_WATCH_SECONDS),
        (30, 7_200.0, 30.0),
    ],
)
def test_a_watch_length_is_the_calls_then_the_persons_then_an_hour(
    raw: object, default: object, seconds: float
) -> None:
    assert watch_seconds(raw, default) == seconds
