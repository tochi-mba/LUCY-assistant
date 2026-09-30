"""The auto-mode audit judges a plan under the same floors as the gate that ran it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from lucy_api.packs.base import Availability, Bound, Catalogue, Permission, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.settings.policy import TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.turn.supervisor import ClaimedTurn, _audit_bypasses

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Any

ACCOUNT = "acct_turn_audit"


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


class Spend:
    id = "gadget"
    title = "Gadget"
    summary = ""

    def permissions(self) -> tuple[Permission, ...]:
        return (
            Permission(
                id="gadget.buy",
                title="Buy something",
                description="Spend money.",
                risk="spend",
                covers=("gadget.buy",),
            ),
        )


async def test_the_audit_reads_the_same_floors_as_the_gate(store: SessionStore) -> None:
    """The bug, named: under `spend_and_destructive_ask`, a spend the person had approved
    was audited as an auto-mode bypass. The audit computed its verdict with the default
    approval policy, under which a spend in auto needs no one, while the gate that had
    actually run asked. Both now read the turn's floors from one place."""
    capabilities = Capabilities((HelpPack(),))
    pack_ctx = capabilities.context_for(
        SessionScope(
            account_id=ACCOUNT, profile="personal", session_id="ses", permission_mode="auto"
        )
    )
    pack_ctx.policy = TurnPolicy(approval_policy="spend_and_destructive_ask")
    pack_ctx.catalogue = Catalogue(
        bound=(
            Bound(
                pack=Spend(),  # type: ignore[arg-type]
                availability=Availability(state=State.ready),
                operations=(SimpleNamespace(name="gadget.buy", effects="write", description=""),),
            ),
        )
    )
    claimed = ClaimedTurn(id="trn", session_id="ses", account_id=ACCOUNT, model="m", thinking="")

    await _audit_bypasses(
        store, claimed, {"steps": [{"op": "gadget.buy", "input": {"sku": "x"}}]}, pack_ctx
    )

    assert await store.audit_log(ACCOUNT) == []
