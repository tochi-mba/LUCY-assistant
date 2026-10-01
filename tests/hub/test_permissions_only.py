"""A standing yes limited to some calls: "always, for this repository".

What is pinned: the limit is matched on the whole value, never a card's cut label; a call
outside it is asked about, never refused; a second limited yes joins the first; a narrow
grant does not hide a wide one beneath it; a deny is never limited; one-shot answers are
untouched; and every surface that records a grant carries the limit.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from conftest import bearer

from lucy_api.core.errors import LucyError
from lucy_api.packs.base import Availability, Bound, Catalogue, State
from lucy_api.packs.repos import ReposPack
from lucy_api.permissions.approvals import ONLY_NEEDS, Ask, answer_approval, open_approval
from lucy_api.permissions.gate import ACCOUNT_PROFILE, Grant, PermissionGate
from lucy_api.permissions.store import (
    ONLY_ALLOWS,
    grants_for,
    list_grants,
    only_of,
    put_grant,
)
from lucy_api.sessions.models import CreateSession, Outcome
from lucy_api.sessions.schema import ADDED_COLUMNS
from lucy_api.sessions.sql_store import identifier
from lucy_api.sessions.turns import close_turn, open_turn
from lucy_api.store.worker import SqlWorker

if TYPE_CHECKING:
    from httpx import AsyncClient

    from lucy_api.sessions.sql_store import SessionStore

OWNER = "acct_only"
LONG = "a-long-organisation-name-of-thirty-nine/" + "r" * 60
"""Longer than a card's 40-character label, which must not be what is matched."""


def catalogue() -> Catalogue:
    pack = ReposPack("http://repos.test")
    bound = Bound(
        pack=pack,
        availability=Availability(state=State.ready),
        operations=tuple(pack.operations(None)),  # type: ignore[arg-type]
    )
    return Catalogue(bound=(bound,))


def merge(repo: str) -> dict[str, Any]:
    return {"steps": [{"id": "m", "op": "repos.merge", "input": {"repo": repo, "number": 1}}]}


def verdict(grants: dict[str, Grant], repo: str, mode: str = "ask") -> Any:
    return PermissionGate().inspect(merge(repo), mode=mode, grants=grants, catalogue=catalogue())


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


def test_an_allow_for_one_repository_runs_there_and_asks_elsewhere() -> None:
    grants = {
        "repos.merge": Grant("repos.merge", "allow", "personal", only=("octo/hello",)),
    }
    assert verdict(grants, "octo/hello").allowed
    elsewhere = verdict(grants, "octo/other")
    assert not elsewhere.allowed
    assert not elsewhere.denied, "outside the limit is not yet asked, never refused"


def test_the_whole_repository_name_is_matched_not_the_cards_label() -> None:
    grants = {"repos.merge": Grant("repos.merge", "allow", "personal", only=(LONG,))}
    assert verdict(grants, LONG).allowed
    assert not verdict(grants, LONG[:-1]).allowed


def test_a_narrow_grant_does_not_hide_the_wide_one_beneath_it() -> None:
    wide = Grant("repos.merge", "allow", ACCOUNT_PROFILE)
    narrow = Grant("repos.merge", "allow", "personal", only=("octo/hello",), wider=wide)
    grants = {"repos.merge": narrow}
    assert verdict(grants, "octo/hello").allowed
    assert verdict(grants, "octo/other").allowed
    assert narrow.covering("octo/other") is wide
    assert Grant("x", "allow", "p", only=("a",)).covering("b") is None


def test_an_unlimited_deny_still_denies_every_repository() -> None:
    grants = {"repos.merge": Grant("repos.merge", "deny", "personal")}
    refused = verdict(grants, "octo/hello", mode="auto")
    assert refused.denied


def test_a_limited_allow_does_not_lift_the_outward_floor_for_another_repository() -> None:
    grants = {"repos.merge": Grant("repos.merge", "allow", "personal", only=("octo/hello",))}
    assert verdict(grants, "octo/hello", mode="auto").allowed
    assert not verdict(grants, "octo/other", mode="auto").allowed


# --------------------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------------------


async def test_two_limited_yeses_join_and_an_unlimited_yes_clears_the_limit(
    sessions_store: SessionStore,
) -> None:
    for repo in ("octo/hello", "octo/other", "octo/hello"):
        await put_grant(
            sessions_store,
            OWNER,
            permission="repos.merge",
            profile="personal",
            decision="allow",
            only=(repo,),
        )
    [row] = await list_grants(sessions_store, OWNER)
    assert row["only"] == ["octo/hello", "octo/other"]

    cleared = await put_grant(
        sessions_store, OWNER, permission="repos.merge", profile="personal", decision="allow"
    )
    assert cleared["only"] == []
    [row] = await list_grants(sessions_store, OWNER)
    assert row["only"] == []


async def test_a_deny_replaces_a_limited_allow_and_cannot_itself_be_limited(
    sessions_store: SessionStore,
) -> None:
    await put_grant(
        sessions_store,
        OWNER,
        permission="repos.merge",
        profile="personal",
        decision="allow",
        only=("octo/hello",),
    )
    with pytest.raises(LucyError) as limited:
        await put_grant(
            sessions_store,
            OWNER,
            permission="repos.merge",
            profile="personal",
            decision="deny",
            only=("octo/hello",),
        )
    assert (limited.value.status, str(limited.value)) == (400, ONLY_ALLOWS)

    await put_grant(
        sessions_store, OWNER, permission="repos.merge", profile="personal", decision="deny"
    )
    grants = await grants_for(sessions_store, OWNER, "personal")
    assert grants["repos.merge"].decision == "deny"
    assert grants["repos.merge"].only == ()


async def test_a_limited_allow_after_a_deny_starts_its_own_limit(
    sessions_store: SessionStore,
) -> None:
    await put_grant(
        sessions_store, OWNER, permission="repos.merge", profile="personal", decision="deny"
    )
    await put_grant(
        sessions_store,
        OWNER,
        permission="repos.merge",
        profile="personal",
        decision="allow",
        only=("octo/hello",),
    )
    grants = await grants_for(sessions_store, OWNER, "personal")
    assert grants["repos.merge"].only == ("octo/hello",)


async def test_the_ledger_overlays_narrow_on_wide_and_keeps_the_wide_beneath(
    sessions_store: SessionStore,
) -> None:
    await put_grant(
        sessions_store, OWNER, permission="repos.merge", profile=ACCOUNT_PROFILE, decision="allow"
    )
    await put_grant(
        sessions_store,
        OWNER,
        permission="repos.merge",
        profile="personal",
        decision="allow",
        only=("octo/hello",),
    )
    grants = await grants_for(sessions_store, OWNER, "personal")
    narrow = grants["repos.merge"]
    assert narrow.only == ("octo/hello",)
    assert narrow.wider is not None
    assert narrow.wider.profile == ACCOUNT_PROFILE
    assert verdict(grants, "octo/anything").allowed


async def test_audit_rows_carry_the_limit_and_nothing_else_of_the_call(
    sessions_store: SessionStore,
) -> None:
    await put_grant(
        sessions_store,
        OWNER,
        permission="repos.merge",
        profile="personal",
        decision="allow",
        only=("octo/hello",),
    )
    rows = await sessions_store.worker.call(
        lambda db: [dict(r) for r in db.execute("SELECT * FROM audit").fetchall()]
    )
    assert any('"only":["octo/hello"]' in str(row.get("detail_json")) for row in rows)


def test_only_reads_back_lists_of_strings_and_nothing_else() -> None:
    assert only_of(None) == ()
    assert only_of("") == ()
    assert only_of('{"a": 1}') == ()
    assert only_of('["octo/a", 3, "", "octo/b"]') == ("octo/a", "octo/b")


async def test_an_existing_database_gains_the_column(tmp_path: Any) -> None:
    path = str(tmp_path / "old.sqlite3")
    # `with sqlite3.connect(...)` commits but never closes: the warning for the leaked
    # connection then lands on whichever test the collector happens to run next.
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE permission_grants (account_id TEXT NOT NULL, profile TEXT NOT NULL, "
            "permission TEXT NOT NULL, decision TEXT NOT NULL, instruction TEXT, "
            "granted_at REAL NOT NULL, source TEXT NOT NULL, "
            "PRIMARY KEY(account_id,profile,permission)) STRICT"
        )
    assert ("permission_grants", "only_json", "TEXT") in ADDED_COLUMNS
    from lucy_api.sessions.sql_store import SessionStore as Store

    worker = SqlWorker(path)
    try:
        await Store(worker).initialize()
        columns = await worker.call(
            lambda db: [row["name"] for row in db.execute("PRAGMA table_info(permission_grants)")]
        )
    finally:
        await worker.aclose()
    assert "only_json" in columns


# --------------------------------------------------------------------------------------
# Answering a card
# --------------------------------------------------------------------------------------


async def parked(store: SessionStore, repo: str = "octo/hello") -> tuple[str, str, str]:
    created = await store.create(OWNER, CreateSession(), identifier("key"))
    session = str(created["id"])
    turn = await open_turn(store, OWNER, session, {"events": [{"type": "input.message"}]})
    await close_turn(store, OWNER, str(turn["id"]), Outcome("running"))
    approval = await open_approval(
        store,
        account=OWNER,
        session_id=session,
        turn_id=str(turn["id"]),
        ask=Ask(
            permission="repos.merge",
            operation="repos.merge",
            description="Merge #1",
            arguments={"repo": repo, "number": 1},
            limit_field="repo",
            limit_values=(repo,),
        ),
    )
    return session, str(turn["id"]), approval


async def test_the_card_offers_the_whole_value_a_limited_yes_would_match(
    sessions_store: SessionStore,
) -> None:
    session, _, _ = await parked(sessions_store, LONG)
    items = await sessions_store.records(OWNER, session, "items")
    [card] = [item for item in items if item["type"] == "approval_request"]
    assert card["content"]["limit"] == {"field": "repo", "values": [LONG]}
    assert len(LONG) > 40, "longer than the label a card counts by"


async def test_always_for_this_repository_is_recorded_from_the_card(
    sessions_store: SessionStore,
) -> None:
    session, _, approval = await parked(sessions_store)

    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {
            "type": "input.approval",
            "approval_id": approval,
            "approved": True,
            "lifetime": "profile",
            "only": ["octo/hello"],
        },
        "always-here",
    )

    grants = await grants_for(sessions_store, OWNER, "personal")
    assert grants["repos.merge"].only == ("octo/hello",)
    items = await sessions_store.records(OWNER, session, "items")
    assert items[-1]["content"]["only"] == ["octo/hello"]


@pytest.mark.parametrize(
    "answer",
    [
        {"approved": True, "lifetime": "once"},
        {"approved": False, "lifetime": "profile"},
    ],
    ids=["once", "denied"],
)
async def test_a_limit_needs_a_standing_yes(
    sessions_store: SessionStore, answer: dict[str, Any]
) -> None:
    session, _, approval = await parked(sessions_store)
    with pytest.raises(LucyError) as refused:
        await answer_approval(
            sessions_store,
            OWNER,
            session,
            {"type": "input.approval", "approval_id": approval, "only": ["octo/hello"], **answer},
            "bad-limit",
        )
    assert str(refused.value) == ONLY_NEEDS


# --------------------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------------------


async def test_put_accepts_a_limit_and_the_listing_shows_it(client: AsyncClient) -> None:
    put = await client.put(
        "/v1/permissions",
        json={
            "permission": "repos.merge",
            "decision": "allow",
            "profile": "personal",
            "only": ["octo/hello"],
        },
        headers=bearer(),
    )
    listed = await client.get("/v1/permissions", headers=bearer())
    refused = await client.put(
        "/v1/permissions",
        json={"permission": "repos.merge", "decision": "deny", "only": ["octo/hello"]},
        headers=bearer(),
    )
    unknown = await client.put(
        "/v1/permissions",
        json={"permission": "repos.merge", "decision": "allow", "for": ["octo/hello"]},
        headers=bearer(),
    )

    assert put.status_code == 200
    assert put.json()["only"] == ["octo/hello"]
    merge_row = next(row for row in listed.json()["data"] if row["id"] == "repos.merge")
    assert merge_row["grant"]["only"] == ["octo/hello"]
    assert merge_row["risk"] == "write"
    assert refused.status_code == 400
    assert unknown.status_code == 422
