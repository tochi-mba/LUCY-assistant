"""Standing consent: recorded while the person is present, used by the turn their work opens.

`Standing` is wiring, so it is tested against the parts it wires: an exchange that records what
it is asked to mint and grant, a verifier, a store and a supervisor. What is pinned is the
order of trust -- consent is a grant for this hub only; a woken turn acts only under a grant
that verifies as the same account; anything unusable makes a turn without authority, never a
crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from lucy_api.auth.exchange import DelegationRefusedError
from lucy_api.auth.verifier import AuthenticationError, VerifiedCaller
from lucy_api.core.container import PackRequest
from lucy_api.core.errors import absent
from lucy_api.core.standing import Standing
from lucy_api.turn.supervisor import PreparedTurn
from lucy_api.work import Kind, Record, State, Team

ACCOUNT = "acct_standing"
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@dataclass
class Exchange:
    refuse: bool = False
    granted: list[dict[str, Any]] = field(default_factory=list)
    minted: list[dict[str, Any]] = field(default_factory=list)
    revoked: list[dict[str, Any]] = field(default_factory=list)

    async def create_delegated_grant(self, **kwargs: Any) -> Any:
        self.granted.append(kwargs)
        return SimpleNamespace(grant_id="dgt_new")

    async def exchange(self, **kwargs: Any) -> Any:
        if self.refuse:
            raise DelegationRefusedError("revoked")
        self.minted.append(kwargs)
        return SimpleNamespace(token=f"minted-under-{kwargs['grant_id']}")

    async def revoke_delegated_grant(self, **kwargs: Any) -> None:
        self.revoked.append(kwargs)


class Verifier:
    def __init__(self, account: str = ACCOUNT) -> None:
        self.account = account
        self.seen: list[str] = []

    async def verify(self, token: str) -> VerifiedCaller:
        self.seen.append(token)
        return VerifiedCaller(account_id=self.account, audience="lucy-api")


class Store:
    def __init__(self, *, gone: bool = False) -> None:
        self.gone = gone

    async def get(self, account: str, session: str) -> dict[str, Any]:
        if self.gone:
            raise absent()
        return {"id": session, "profile": "work", "permission_mode": "auto", "incognito": 0}


class Container:
    def __init__(self, **parts: Any) -> None:
        self.settings = SimpleNamespace(audience="lucy-api")
        self.exchange = parts.get("exchange", Exchange())
        self.verifier = parts.get("verifier", Verifier())
        self.store = parts.get("store", Store())
        self.prepared: list[tuple[PackRequest, dict[str, Any]]] = []
        self.turns = SimpleNamespace(authorized=[])
        self.turns.authorize = lambda turn_id, prepared: self.turns.authorized.append(
            (turn_id, prepared)
        )

    async def prepare_turn(self, request: PackRequest, session: dict[str, Any]) -> PreparedTurn:
        self.prepared.append((request, session))
        return PreparedTurn(pack_context=SimpleNamespace(), live=SimpleNamespace())  # type: ignore[arg-type]


def a_subscription(**tags: str) -> Record:
    return Record(
        id="wrk_sub",
        kind=Kind.subscription,
        role="repos",
        objective="Merge #42 when CI is green",
        session_id="ses_1",
        started_at=START,
        state=State.succeeded,
        account_id=ACCOUNT,
        wake=True,
        tags={"capability": "repos", "subscription": "sub_1", **tags},
    )


def a_request() -> PackRequest:
    return PackRequest(
        caller=VerifiedCaller(account_id=ACCOUNT, audience="lucy-api"),
        user_token="the-persons-token",
        profile="work",
        session_id="ses_1",
    )


async def test_consent_is_a_grant_for_this_hub_alone_for_the_lifetime_asked() -> None:
    container = Container()
    standing = Standing(container)  # type: ignore[arg-type]

    grant = await standing.consent(a_request(), 4500.7)

    assert grant == "dgt_new"
    assert container.exchange.granted == [
        {
            "profile": "work",
            "user_token": "the-persons-token",
            "audiences": ("lucy-api",),
            "ttl_seconds": 4500,
        }
    ]


def test_only_a_subscription_opened_with_consent_carries_any() -> None:
    standing = Standing(Container())  # type: ignore[arg-type]
    assert standing.carries(a_subscription(grant="dgt_1"))
    assert not standing.carries(a_subscription())
    helper = a_subscription(grant="dgt_1")
    helper.kind = Kind.helper
    assert not standing.carries(helper)
    assert not standing.carries(Team(session_id="ses_1", group="g", members=()))


async def test_a_woken_turn_is_prepared_as_a_persons_turn_under_a_token_from_the_grant() -> None:
    container = Container()
    standing = Standing(container)  # type: ignore[arg-type]

    prepared = await standing.prepare(a_subscription(grant="dgt_1"))

    assert isinstance(prepared, PreparedTurn)
    assert container.exchange.minted == [{"audience": "lucy-api", "grant_id": "dgt_1"}]
    assert container.verifier.seen == ["minted-under-dgt_1"]
    [(request, session)] = container.prepared
    assert request.user_token == "minted-under-dgt_1"
    assert request.caller.account_id == ACCOUNT
    assert (request.profile, request.session_id) == ("work", "ses_1")
    assert (request.permission_mode, request.incognito) == ("auto", False)
    assert session["id"] == "ses_1"

    standing.authorize("trn_1", prepared)
    assert container.turns.authorized == [("trn_1", prepared)]


async def test_anything_else_handed_to_authorize_is_ignored() -> None:
    container = Container()
    Standing(container).authorize("trn_1", "not a prepared turn")  # type: ignore[arg-type]
    assert container.turns.authorized == []


async def test_a_team_lends_nothing() -> None:
    standing = Standing(Container())  # type: ignore[arg-type]
    assert await standing.prepare(Team(session_id="ses_1", group="g", members=())) is None


@pytest.mark.parametrize(
    "parts",
    [
        {"exchange": Exchange(refuse=True)},
        {"verifier": Verifier(account="acct_someone_else")},
        {"store": Store(gone=True)},
    ],
    ids=["revoked", "another-account", "session-gone"],
)
async def test_consent_that_cannot_be_used_lends_nothing_and_says_so_in_the_log(
    parts: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    container = Container(**parts)

    prepared = await Standing(container).prepare(a_subscription(grant="dgt_1"))  # type: ignore[arg-type]

    assert prepared is None
    assert container.prepared == []
    assert "standing_consent_unusable" in caplog.text
    assert "minted-under" not in caplog.text


async def test_withdrawing_consent_uses_the_grant_to_give_itself_up() -> None:
    container = Container()

    await Standing(container).withdraw(  # type: ignore[arg-type]
        {"grant_id": "dgt_9", "profile": "work"}
    )

    assert container.exchange.minted == [{"audience": "lucy-api", "grant_id": "dgt_9"}]
    assert container.exchange.revoked == [
        {"profile": "work", "user_token": "minted-under-dgt_9", "grant_id": "dgt_9"}
    ]


async def test_withdrawing_without_a_grant_does_nothing() -> None:
    container = Container()
    await Standing(container).withdraw({"grant_id": None, "profile": "work"})  # type: ignore[arg-type]
    assert container.exchange.minted == []


def test_a_grant_for_another_account_is_an_authentication_failure_by_name() -> None:
    from lucy_api.core.standing import NOT_THIS_ACCOUNT

    assert str(AuthenticationError(NOT_THIS_ACCOUNT)) == "the grant is for another account"


# --------------------------------------------------------------------------------------
# Serving capabilities that watch through a sibling
# --------------------------------------------------------------------------------------


class Hooks:
    """The two registrations `serve` makes on `Subscriptions`, recorded by capability."""

    def __init__(self) -> None:
        self.release: dict[str, Any] = {}
        self.check: dict[str, Any] = {}

    def on_release(self, capability: str, handler: Any) -> None:
        self.release[capability] = handler

    def on_check(self, capability: str, handler: Any) -> None:
        self.check[capability] = handler


class Watching:
    """A capability that watches through its sibling, recording the seam it was handed."""

    id = "watching"

    def __init__(self) -> None:
        self.seen: list[tuple[str, Any, dict[str, Any]]] = []

    async def release_subscription(self, http: Any, row: Any) -> None:
        self.seen.append(("release", http, dict(row)))

    async def check_subscription(self, http: Any, row: Any) -> str:
        self.seen.append(("check", http, dict(row)))
        return "checked"


class HalfWatching:
    id = "half"

    async def release_subscription(self, http: Any, row: Any) -> None:
        raise AssertionError


class Nameless(Watching):
    id = ""


def served(client: Any) -> tuple[Any, Hooks, Watching]:
    container = client._transport.app.state.container
    hooks = Hooks()
    pack = Watching()
    container.standing.serve(hooks, (pack, HalfWatching(), Nameless(), object()))
    return container, hooks, pack


async def test_only_a_capability_with_both_hooks_and_a_name_is_served(client: Any) -> None:
    _, hooks, _ = served(client)
    assert set(hooks.release) == set(hooks.check) == {"watching"}


async def test_a_subscription_without_consent_never_reaches_its_sibling(client: Any) -> None:
    _, hooks, pack = served(client)
    row = {"grant_id": "", "account_id": ACCOUNT, "profile": "work"}
    assert await hooks.check["watching"](row) is None
    assert await hooks.release["watching"](row) is None
    assert pack.seen == []


async def test_a_consented_subscription_reaches_its_sibling_as_its_person(client: Any) -> None:
    from lucy_api.packs.probes import GuardedHttp

    container, hooks, pack = served(client)
    row = {"grant_id": "dgt_1", "account_id": ACCOUNT, "profile": "work"}
    assert await hooks.check["watching"](row) == "checked"
    await hooks.release["watching"](row)

    [(first, http, seen), (second, _, _)] = pack.seen
    assert (first, second) == ("check", "release")
    assert seen == row
    assert isinstance(http, GuardedHttp)
    assert (http._account_id, http._profile) == (ACCOUNT, "work")

    forgotten: list[tuple[str, str]] = []
    container.capabilities.forget_probes = lambda account, profile: forgotten.append(
        (account, profile)
    )
    http._on_disconnect()
    assert forgotten == [(ACCOUNT, "work")]
