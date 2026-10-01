"""Subscriptions: work a sibling finishes and signals, held durably by the hub.

The properties: opening one writes a row and starts a record in the one work registry; a
signed signal ends it once, as the registry ends anything; a signal that does not verify is
indistinguishable from one for a subscription that does not exist; a restart takes up what
was open under the same work id; a sweep finds an ending whose signal was lost; a cancel
releases what the subscription held, and nothing else does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.core.errors import LucyError
from lucy_api.net.signing import sign
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.work import Kind, State
from lucy_api.work.registry import Registry
from lucy_api.work.subscriptions import (
    ENDED,
    MALFORMED,
    MAX_LIFETIME_SECONDS,
    MAX_SIGNAL_BYTES,
    STANDING_MARGIN_SECONDS,
    TOO_LARGE,
    Signal,
    Subscriptions,
    SubscriptionSeam,
    parse_signal,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

ACCOUNT = "acct_subs"
SIGNALS = "http://lucy.test/v1/signals/"


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def registry() -> Registry:
    return Registry(now=lambda: datetime.now(UTC))


async def a_session(store: SessionStore) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    return str(created["id"])


async def settled() -> None:
    """Let endings reach their listeners, which write rows on the database thread."""
    for _ in range(20):
        await asyncio.sleep(0.005)


async def opened_one(subscriptions: Subscriptions, session_id: str, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "account_id": ACCOUNT,
        "session_id": session_id,
        "profile": "personal",
        "capability": "repos",
        "objective": "Say when CI on #42 is green",
        "timeout_seconds": 3600,
    }
    return await subscriptions.open(**{**fields, **overrides})


def signed(secret: str, body: Mapping[str, Any]) -> tuple[str, bytes]:
    raw = json.dumps(body).encode()
    return sign(secret, raw), raw


async def rows(store: SessionStore) -> list[dict[str, Any]]:
    def read(db: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in db.execute("SELECT * FROM subscriptions").fetchall()]

    return await store.worker.call(read)


# --------------------------------------------------------------------------------------
# Opening
# --------------------------------------------------------------------------------------


async def test_opening_writes_a_row_and_starts_a_running_subscription_record(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    session = await a_session(store)

    opened = await opened_one(subscriptions, session, grant_id="dgt_1")

    assert opened.signal_url == f"http://lucy.test/v1/signals/{opened.subscription_id}"
    assert opened.secret
    assert opened.secret not in repr(opened)
    [record] = work.running(session)
    assert record.id == opened.handle.id
    assert record.kind is Kind.subscription
    assert record.wake is True
    assert record.tags == {
        "capability": "repos",
        "subscription": opened.subscription_id,
        "grant": "dgt_1",
    }
    [row] = await rows(store)
    assert row["state"] == "running"
    assert row["work_id"] == opened.handle.id
    assert row["grant_id"] == "dgt_1"
    await work.shutdown()


async def test_a_lifetime_is_bounded_on_both_sides(store: SessionStore) -> None:
    work = registry()
    clock = Clock()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS, clock=clock)
    session = await a_session(store)

    await opened_one(subscriptions, session, timeout_seconds=10 * MAX_LIFETIME_SECONDS)
    await opened_one(subscriptions, session, timeout_seconds=0)

    lives = sorted(row["expires_at"] - row["created_at"] for row in await rows(store))
    assert lives == [1.0, MAX_LIFETIME_SECONDS]
    await work.shutdown()


async def test_attach_records_the_siblings_own_id(store: SessionStore) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store))

    await subscriptions.attach(opened.subscription_id, "gh_sub_9")

    [row] = await rows(store)
    assert row["sibling_id"] == "gh_sub_9"
    await work.shutdown()


# --------------------------------------------------------------------------------------
# Ending by signal
# --------------------------------------------------------------------------------------


async def test_a_fired_signal_ends_the_work_as_succeeded_with_the_signal_as_its_result(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store))
    signature, body = signed(
        opened.secret,
        {
            "state": "fired",
            "summary": "  CI on   #42 is green ",
            "facts": {"conclusion": "success", "checks": 7},
            "excerpt": "all 7 checks passed",
        },
    )

    await subscriptions.signal(opened.subscription_id, signature, body)
    await settled()

    result = work.result(opened.handle.id)
    assert result.state is State.succeeded
    assert result.payload == {
        "state": "fired",
        "summary": "CI on #42 is green",
        "facts": {"conclusion": "success", "checks": 7},
        "excerpt": "all 7 checks passed",
    }
    [row] = await rows(store)
    assert row["state"] == "succeeded"
    assert json.loads(row["result_json"])["summary"] == "CI on #42 is green"
    assert row["ended_at"] is not None


async def test_a_failed_signal_ends_as_failed_with_its_summary_and_an_expired_one_as_timed_out(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    session = await a_session(store)
    failing = await opened_one(subscriptions, session)
    expiring = await opened_one(subscriptions, session)

    for opened, state, summary in (
        (failing, "failed", "the run was cancelled upstream"),
        (expiring, "expired", "no run started in time"),
    ):
        signature, body = signed(opened.secret, {"state": state, "summary": summary})
        await subscriptions.signal(opened.subscription_id, signature, body)
    await settled()

    failed = work.result(failing.handle.id)
    assert failed.state is State.failed
    assert failed.detail == "the run was cancelled upstream"
    assert failed.payload == {"state": "failed", "summary": "the run was cancelled upstream"}
    assert work.result(expiring.handle.id).state is State.timed_out
    states = sorted(row["state"] for row in await rows(store))
    assert states == ["failed", "timed_out"]


async def test_a_bad_signature_and_an_unknown_subscription_are_the_same_not_found(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store))
    _, body = signed(opened.secret, {"state": "fired", "summary": "done"})

    refusals = []
    for subscription_id, signature in (
        (opened.subscription_id, sign("not-the-secret", body)),
        (opened.subscription_id, None),
        ("sub_never_existed", sign(opened.secret, body)),
    ):
        with pytest.raises(LucyError) as refused:
            await subscriptions.signal(subscription_id, signature, body)
        refusals.append((refused.value.status, refused.value.code, str(refused.value)))

    assert len(set(refusals)) == 1
    assert refusals[0][0] == 404
    assert work.state_of(opened.handle.id) is State.running
    await work.shutdown()


async def test_a_second_signal_is_a_conflict_and_changes_nothing(store: SessionStore) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store))
    first = signed(opened.secret, {"state": "fired", "summary": "green"})
    second = signed(opened.secret, {"state": "failed", "summary": "red after all"})

    await subscriptions.signal(opened.subscription_id, *first)
    with pytest.raises(LucyError) as again:
        await subscriptions.signal(opened.subscription_id, *second)
    await settled()

    assert again.value.status == 409
    assert str(again.value) == ENDED
    assert work.result(opened.handle.id).state is State.succeeded


async def test_a_signal_over_the_cap_is_refused_before_it_is_read(store: SessionStore) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store))
    body = b"{" + b" " * MAX_SIGNAL_BYTES + b"}"

    with pytest.raises(LucyError) as refused:
        await subscriptions.signal(opened.subscription_id, sign(opened.secret, body), body)

    assert (refused.value.status, str(refused.value)) == (413, TOO_LARGE)
    await work.shutdown()


async def test_a_verified_body_that_is_not_a_signal_says_what_one_looks_like(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store))
    body = b'{"state": "maybe", "summary": "?"}'

    with pytest.raises(LucyError) as refused:
        await subscriptions.signal(opened.subscription_id, sign(opened.secret, body), body)

    assert (refused.value.status, str(refused.value)) == (422, MALFORMED)
    assert work.state_of(opened.handle.id) is State.running
    await work.shutdown()


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"state": "fired"}',
        b'{"state": "fired", "summary": 3}',
        b'{"state": "fired", "summary": "x", "facts": []}',
        b'{"state": "fired", "summary": "x", "excerpt": 1}',
        b'{"state": "fired", "summary": "x", "result": {"whole": "page"}}',
    ],
)
def test_parse_refuses_every_shape_that_is_not_a_signal(body: bytes) -> None:
    with pytest.raises(LucyError) as refused:
        parse_signal(body)
    assert refused.value.status == 422


def test_parse_keeps_small_facts_only_and_bounds_the_rest() -> None:
    facts: dict[str, Any] = {f"f{index}": index for index in range(20)}
    facts["f0"] = "x" * 500
    facts["f1"] = {"nested": True}
    facts["f2"] = None
    facts["f3"] = 1.5
    signal = parse_signal(
        json.dumps(
            {"state": "fired", "summary": "y" * 500, "facts": facts, "excerpt": "z" * 5000}
        ).encode()
    )

    assert len(signal.summary) == 120
    assert len(signal.facts) == 11  # twelve kept, and the nested one of them dropped
    assert signal.facts["f0"] == "x" * 120
    assert "f1" not in signal.facts
    assert signal.facts["f2"] is None
    assert signal.facts["f3"] == 1.5
    assert signal.excerpt.endswith("[…]")
    assert len(signal.excerpt) == 1_500


def test_a_signal_without_facts_or_excerpt_reports_neither() -> None:
    assert Signal(state="fired", summary="done").payload() == {"state": "fired", "summary": "done"}


# --------------------------------------------------------------------------------------
# Endings the registry decides
# --------------------------------------------------------------------------------------


async def test_cancelling_releases_through_the_capability_and_withdraws_consent(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    released: list[tuple[str, str]] = []

    async def release(row: Mapping[str, Any]) -> None:
        released.append(("repos", str(row["sibling_id"])))

    async def withdraw(row: Mapping[str, Any]) -> None:
        released.append(("consent", str(row["grant_id"])))

    subscriptions.on_release("repos", release)
    subscriptions.on_consent_release(withdraw)
    opened = await opened_one(subscriptions, await a_session(store), grant_id="dgt_7")
    await subscriptions.attach(opened.subscription_id, "gh_sub_1")

    work.cancel(opened.handle.id)
    await settled()

    assert released == [("repos", "gh_sub_1"), ("consent", "dgt_7")]
    [row] = await rows(store)
    assert row["state"] == "cancelled"
    signature, body = signed(opened.secret, {"state": "fired", "summary": "late"})
    with pytest.raises(LucyError) as late:
        await subscriptions.signal(opened.subscription_id, signature, body)
    assert late.value.status == 409


async def test_a_release_that_fails_is_logged_and_does_not_stop_the_next(
    store: SessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    withdrawn: list[str] = []

    async def broken(row: Mapping[str, Any]) -> None:
        raise ConnectionError(str(row["secret"]))

    async def withdraw(row: Mapping[str, Any]) -> None:
        withdrawn.append(str(row["grant_id"]))

    subscriptions.on_release("repos", broken)
    subscriptions.on_consent_release(withdraw)
    opened = await opened_one(subscriptions, await a_session(store), grant_id="dgt_8")

    work.cancel(opened.handle.id)
    await settled()

    assert withdrawn == ["dgt_8"]
    assert "subscription_release_failed" in caplog.text
    assert opened.secret not in caplog.text


async def test_without_consent_or_a_registered_release_a_cancel_only_ends_the_row(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store), capability="elsewhere")

    work.cancel(opened.handle.id)
    await settled()

    [row] = await rows(store)
    assert row["state"] == "cancelled"


async def test_an_expiry_ends_the_row_timed_out_and_releases_nothing(store: SessionStore) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    released: list[str] = []

    async def release(row: Mapping[str, Any]) -> None:
        released.append(str(row["id"]))

    subscriptions.on_release("repos", release)
    opened = await opened_one(subscriptions, await a_session(store), timeout_seconds=1)
    await asyncio.sleep(1.05)
    await settled()

    assert work.state_of(opened.handle.id) is State.timed_out
    assert "expired after" in work.result(opened.handle.id).detail
    assert released == []
    [row] = await rows(store)
    assert row["state"] == "timed_out"


async def test_other_kinds_of_work_ending_touch_no_row(store: SessionStore) -> None:
    from lucy_api.work import Brief

    work = registry()
    Subscriptions(store, work, signal_base_url=SIGNALS)

    async def done() -> str:
        return "ok"

    handle = work.start(done(), Brief(session_id="ses_x", kind=Kind.job, role="j", objective="o"))
    await settled()

    assert work.state_of(handle.id) is State.succeeded
    assert await rows(store) == []


# --------------------------------------------------------------------------------------
# After a restart
# --------------------------------------------------------------------------------------


async def test_a_restart_leaves_the_row_open_and_the_next_process_takes_it_up(
    store: SessionStore,
) -> None:
    clock = Clock()
    before = registry()
    first = Subscriptions(store, before, signal_base_url=SIGNALS, clock=clock)
    session = await a_session(store)
    opened = await opened_one(first, session, grant_id="dgt_2")
    told: list[str] = []

    async def listener(record: Any) -> None:
        told.append(record.id)

    before.on_finished(listener)
    await before.shutdown()
    await settled()

    assert told == []  # a restart is not this subscription's ending to announce
    [row] = await rows(store)
    assert row["state"] == "running"

    clock.now += 600
    after = registry()
    second = Subscriptions(store, after, signal_base_url=SIGNALS, clock=clock)
    assert await second.restore() == 1

    [record] = after.running(session)
    assert record.id == opened.handle.id
    assert record.kind is Kind.subscription
    assert record.timeout_seconds == pytest.approx(3000)
    assert record.tags["grant"] == "dgt_2"
    signature, body = signed(opened.secret, {"state": "fired", "summary": "green"})
    await second.signal(opened.subscription_id, signature, body)
    await settled()
    assert after.result(opened.handle.id).state is State.succeeded


async def test_one_past_its_deadline_when_taken_up_ends_timed_out_at_once(
    store: SessionStore,
) -> None:
    clock = Clock()
    before = registry()
    first = Subscriptions(store, before, signal_base_url=SIGNALS, clock=clock)
    session = await a_session(store)
    opened = await opened_one(first, session, timeout_seconds=60)
    await before.shutdown()

    clock.now += 3600
    after = registry()
    await Subscriptions(store, after, signal_base_url=SIGNALS, clock=clock).restore()
    await asyncio.sleep(0.05)
    await settled()

    assert after.state_of(opened.handle.id) is State.timed_out


# --------------------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------------------


async def test_the_sweep_ends_what_a_sibling_says_has_ended_and_skips_the_rest(
    store: SessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    session = await a_session(store)
    fired = await opened_one(subscriptions, session)
    waiting = await opened_one(subscriptions, session)
    unreachable = await opened_one(subscriptions, session)
    unattached = await opened_one(subscriptions, session)
    unknown = await opened_one(subscriptions, session, capability="nobody-checks")
    for opened in (fired, waiting, unreachable, unknown):
        await subscriptions.attach(opened.subscription_id, "sib_" + opened.subscription_id)

    async def check(row: Mapping[str, Any]) -> Signal | None:
        if row["id"] == fired.subscription_id:
            return Signal(state="fired", summary="green while you were away")
        if row["id"] == unreachable.subscription_id:
            raise ConnectionError
        return None

    subscriptions.on_check("repos", check)

    assert await subscriptions.sweep() == 1
    await settled()

    assert work.result(fired.handle.id).payload == {
        "state": "fired",
        "summary": "green while you were away",
    }
    for opened in (waiting, unreachable, unattached, unknown):
        assert work.state_of(opened.handle.id) is State.running
    assert "subscription_sweep_failed" in caplog.text
    await work.shutdown()


async def test_a_sweep_racing_a_signal_counts_only_the_ending_it_made(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    opened = await opened_one(subscriptions, await a_session(store))
    await subscriptions.attach(opened.subscription_id, "sib")

    async def check(row: Mapping[str, Any]) -> Signal:
        signature, body = signed(opened.secret, {"state": "fired", "summary": "signal first"})
        await subscriptions.signal(opened.subscription_id, signature, body)
        return Signal(state="fired", summary="sweep second")

    subscriptions.on_check("repos", check)

    assert await subscriptions.sweep() == 0
    await settled()
    assert work.result(opened.handle.id).payload == {"state": "fired", "summary": "signal first"}


async def test_run_sweeps_sweeps_on_its_interval_until_cancelled(store: SessionStore) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    swept: list[int] = []

    async def counting() -> int:
        swept.append(1)
        return 0

    subscriptions.sweep = counting  # type: ignore[method-assign]
    task = asyncio.create_task(subscriptions.run_sweeps(0.001))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(swept) >= 2


# --------------------------------------------------------------------------------------
# The seam a turn's capabilities use
# --------------------------------------------------------------------------------------


async def test_a_seam_records_consent_for_a_waking_subscription_and_none_otherwise(
    store: SessionStore,
) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    session = await a_session(store)
    asked: list[float] = []

    async def consent(lifetime: float) -> str:
        asked.append(lifetime)
        return "dgt_seam"

    seam = SubscriptionSeam(
        subscriptions, account_id=ACCOUNT, session_id=session, profile="work", consent=consent
    )

    waking = await seam.open(capability="repos", objective="Say when", timeout_seconds=600)
    quiet = await seam.open(
        capability="repos", objective="Note when", timeout_seconds=600, wake=False
    )
    await seam.attach(waking, "sib_1")

    assert asked == [600 + STANDING_MARGIN_SECONDS]
    by_id = {row["id"]: row for row in await rows(store)}
    assert by_id[waking.subscription_id]["grant_id"] == "dgt_seam"
    assert by_id[waking.subscription_id]["profile"] == "work"
    assert by_id[waking.subscription_id]["sibling_id"] == "sib_1"
    assert by_id[quiet.subscription_id]["grant_id"] is None
    await work.shutdown()


async def test_consent_that_cannot_be_recorded_still_opens_the_subscription(
    store: SessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)

    async def refused(lifetime: float) -> str:
        raise PermissionError(str(lifetime))

    seam = SubscriptionSeam(
        subscriptions,
        account_id=ACCOUNT,
        session_id=await a_session(store),
        profile="personal",
        consent=refused,
    )
    opened = await seam.open(capability="repos", objective="Say when", timeout_seconds=60)

    [row] = await rows(store)
    assert row["grant_id"] is None
    assert "standing_consent_failed" in caplog.text
    assert work.state_of(opened.handle.id) is State.running
    await work.shutdown()


async def test_a_seam_without_consent_opens_without_asking(store: SessionStore) -> None:
    work = registry()
    subscriptions = Subscriptions(store, work, signal_base_url=SIGNALS)
    seam = SubscriptionSeam(
        subscriptions, account_id=ACCOUNT, session_id=await a_session(store), profile="personal"
    )

    opened = await seam.open(capability="repos", objective="Say when", timeout_seconds=60)
    seam.abandon(opened)
    await settled()

    assert work.state_of(opened.handle.id) is State.cancelled
    [row] = await rows(store)
    assert row["grant_id"] is None
    assert row["state"] == "cancelled"


async def test_a_signal_for_a_row_this_process_has_not_taken_up_still_ends_the_row(
    store: SessionStore,
) -> None:
    before = registry()
    first = Subscriptions(store, before, signal_base_url=SIGNALS)
    opened = await opened_one(first, await a_session(store))
    await before.shutdown()

    second = Subscriptions(store, registry(), signal_base_url=SIGNALS)
    signature, body = signed(opened.secret, {"state": "fired", "summary": "green"})
    await second.signal(opened.subscription_id, signature, body)

    [row] = await rows(store)
    assert row["state"] == "succeeded"
    assert await second.restore() == 0
