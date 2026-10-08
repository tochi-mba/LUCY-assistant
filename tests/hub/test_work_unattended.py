"""What Lucy may do when work ends and nobody is talking, as the person's settings say.

Four settings reach the work: `act_unattended` decides whether a subscription that wakes the
session asks for standing consent, and whether the woken turn may use any; `quiet_hours` rides
on the work so the waker can hold its turn back; `wake_by_default` and `watch_default_minutes`
are what a watch falls to when the model does not say. Each is pinned here where it lands: the
subscription seam, the waker's line, the watch capability, and the container that reads them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from conftest import build_settings
from keyring_client.testing import FakeKeyring
from settings_client.testing import FakeSettingsClient
from test_watch_pack import Clock as WatchClock
from test_watch_pack import Web, a_context, a_pack, run, start, step

from lucy_api.auth.verifier import VerifiedCaller
from lucy_api.core.container import PackRequest, build_container
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.settings.policy import TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.work import Kind, Record, Registry, State, Waker, wake_line
from lucy_api.work.quiet import QUIET_TAG, QuietHours
from lucy_api.work.subscriptions import (
    REPORT_ONLY_ADVICE,
    STANDING_MARGIN_SECONDS,
    Subscriptions,
    SubscriptionSeam,
    governed,
)
from lucy_api.work.wake import CONSENT_TAG, NO_STANDING, WITHHELD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

ACCOUNT = "acct_unattended"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
REPORT_ONLY = TurnPolicy(act_unattended=False)
CONSENTING = TurnPolicy(act_unattended=True)


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    return str(created["id"])


class Seams:
    """A real subscription store, and a seam over it whose consent is recorded, not kept."""

    def __init__(self, store: SessionStore) -> None:
        self.registry = Registry(now=lambda: NOW)
        self.subscriptions = Subscriptions(
            store,
            self.registry,
            signal_base_url="http://lucy.test/v1/signals",
            clock=NOW.timestamp,
        )
        self.consents: list[float] = []

    async def consent(self, lifetime: float) -> str:
        self.consents.append(lifetime)
        return "dgt_1"

    def seam(self, session: str) -> SubscriptionSeam:
        return SubscriptionSeam(
            self.subscriptions,
            account_id=ACCOUNT,
            session_id=session,
            profile="personal",
            consent=self.consent,
        )

    async def close(self) -> None:
        await self.subscriptions.aclose()
        await self.registry.shutdown()


# --------------------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------------------


async def test_with_act_unattended_off_a_waking_subscription_asks_no_consent(
    store: SessionStore,
) -> None:
    """New behaviour: no grant is recorded, and the work says why it carries none."""
    seams = Seams(store)
    session = await a_session(store)
    seam = seams.seam(session).under(REPORT_ONLY)

    opened = await seam.open(
        capability="repos", objective="Merge #42 when CI is green", timeout_seconds=600
    )

    assert seams.consents == []
    [row] = await seams.subscriptions.open_rows()
    assert row["grant_id"] is None
    assert row["tags"] == {CONSENT_TAG: WITHHELD}
    [record] = seams.registry.running(session)
    assert record.id == opened.handle.id
    assert record.tags[CONSENT_TAG] == WITHHELD
    assert "grant" not in record.tags
    assert seam.advice() == REPORT_ONLY_ADVICE
    await seams.close()


async def test_a_waking_commands_tags_record_the_same_consent_a_subscription_gets(
    store: SessionStore,
) -> None:
    """The bug, named: only subscriptions recorded standing consent, so the turn a finished
    background command opened ran with no authority and none of the person's settings."""
    seams = Seams(store)
    session = await a_session(store)
    seam = seams.seam(session).under(CONSENTING)

    tags = await seam.standing_tags(240)

    assert tags == {"grant": "dgt_1"}
    assert seams.consents == [240 + STANDING_MARGIN_SECONDS], "for as long as the work may live"
    await seams.close()


async def test_with_act_unattended_off_a_waking_command_says_why_it_carries_none(
    store: SessionStore,
) -> None:
    seams = Seams(store)
    session = await a_session(store)
    seam = seams.seam(session).under(REPORT_ONLY)

    assert await seam.standing_tags(240) == {CONSENT_TAG: WITHHELD}
    assert seams.consents == []
    await seams.close()


async def test_consent_that_cannot_be_recorded_leaves_the_work_bare(
    store: SessionStore,
) -> None:
    seams = Seams(store)
    session = await a_session(store)
    seam = seams.seam(session).under(CONSENTING)

    async def refused(lifetime: float) -> str:
        raise RuntimeError("keyring is down")

    seam._consent = refused

    assert await seam.standing_tags(240) == {}
    await seams.close()


async def test_a_checkin_under_act_unattended_off_asks_no_consent_either(
    store: SessionStore,
) -> None:
    seams = Seams(store)
    session = await a_session(store)
    seam = seams.seam(session).under(REPORT_ONLY)

    await seam.checkin(
        objective="Look at the pull request again", due_at=NOW.timestamp() + 600, delay_seconds=600
    )

    assert seams.consents == []
    [row] = await seams.subscriptions.open_rows()
    assert (row["grant_id"], row["tags"]) == (None, {CONSENT_TAG: WITHHELD})
    await seams.close()


async def test_under_the_defaults_the_seam_is_exactly_what_it_was(store: SessionStore) -> None:
    """New behaviour, by its absence: nobody choosing anything changes nothing."""
    seams = Seams(store)
    session = await a_session(store)
    seam = seams.seam(session).under(TurnPolicy())

    await seam.open(capability="repos", objective="Merge #42", timeout_seconds=600)

    assert len(seams.consents) == 1
    [row] = await seams.subscriptions.open_rows()
    assert (row["grant_id"], row["tags"]) == ("dgt_1", None)
    assert seam.advice() == ""
    assert seam.quiet is None
    await seams.close()


async def test_quiet_hours_ride_on_what_the_seam_opens_and_on_what_it_advises(
    store: SessionStore,
) -> None:
    seams = Seams(store)
    session = await a_session(store)
    policy = TurnPolicy(act_unattended=False, quiet_hours="23:00-07:00")
    seam = seams.seam(session).under(policy)

    await seam.open(capability="repos", objective="Merge #42", timeout_seconds=600)

    [row] = await seams.subscriptions.open_rows()
    assert row["tags"] == {CONSENT_TAG: WITHHELD, QUIET_TAG: "23:00-07:00@UTC"}
    assert seam.advice() == REPORT_ONLY_ADVICE + QuietHours(23 * 60, 7 * 60, "UTC").advice()
    await seams.close()


def test_a_turn_without_a_subscription_store_still_has_none() -> None:
    assert governed(None, REPORT_ONLY) is None


# --------------------------------------------------------------------------------------
# The woken turn's line
# --------------------------------------------------------------------------------------


class Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


class NeverLends:
    def carries(self, ending: Any) -> bool:
        return False

    async def prepare(self, ending: Any) -> object | None:
        raise AssertionError

    def authorize(self, turn_id: str, prepared: object) -> None:
        raise AssertionError


def a_subscription(session: str, **tags: str) -> Record:
    return Record(
        id="wrk_sub",
        kind=Kind.subscription,
        role="repos",
        objective="Merge #42 when CI is green",
        session_id=session,
        started_at=NOW,
        finished_at=NOW,
        state=State.succeeded,
        account_id=ACCOUNT,
        wake=True,
        tags={"capability": "repos", "subscription": "sub_1", **tags},
    )


@pytest.mark.parametrize("authority", [None, NeverLends()])
async def test_work_opened_with_consent_withheld_wakes_a_turn_told_it_may_only_report(
    store: SessionStore, authority: NeverLends | None
) -> None:
    """New behaviour: the model reads that it runs without consent, and why that means asking."""
    waker = Waker(store, EventEmitter(SqlEventLog(store), Snapshot()), authority=authority)
    session = await a_session(store)
    record = a_subscription(session, **{CONSENT_TAG: WITHHELD})

    await waker.on_finished(record)

    [item] = await store.records(ACCOUNT, session, "items")
    assert item["content"] == wake_line(record)[:-1] + NO_STANDING + "]"


# --------------------------------------------------------------------------------------
# The watch capability
# --------------------------------------------------------------------------------------


def chosen(**values: Any) -> TurnPolicy:
    return TurnPolicy(**values)


async def test_a_watch_falls_to_the_persons_lifetime_and_wake_when_the_model_says_neither() -> None:
    """New behaviour: `watch_default_minutes` and `wake_by_default`, in the call and its schema."""
    registry = Registry(now=WatchClock())
    context = a_context(registry)
    context.policy = chosen(watch_default_minutes=20, wake_by_default=False)
    pack = a_pack(fetch=Web())

    started = step(await run(pack, start({"url": "https://example.com/ready"}), context))

    [record] = registry.running(context.session_id)
    assert (started["for_seconds"], started["wake"]) == (1200, False)
    assert (record.timeout_seconds, record.wake, record.tags) == (1200, False, {})
    [operation] = [op for op in pack.operations(context) if op.name == "watch.start"]
    described = str(operation.input)
    assert "1200 by default" in described
    assert "Default false." in described
    await registry.shutdown()


async def test_a_watch_the_model_asks_to_wake_carries_the_quiet_hours_and_says_so() -> None:
    registry = Registry(now=WatchClock())
    context = a_context(registry)
    context.policy = chosen(wake_by_default=False, quiet_hours="23:00-07:00")
    pack = a_pack(fetch=Web())

    started = step(
        await run(
            pack,
            start({"url": "https://example.com/ready", "wake": True, "for_seconds": 90}),
            context,
        )
    )

    [record] = registry.running(context.session_id)
    assert started["for_seconds"] == 90
    assert record.tags == {QUIET_TAG: "23:00-07:00@UTC"}
    assert started["advice"].endswith(QuietHours(23 * 60, 7 * 60, "UTC").advice())
    await registry.shutdown()


# --------------------------------------------------------------------------------------
# The container reads them, for this person and profile
# --------------------------------------------------------------------------------------


async def test_a_turn_prepared_under_act_unattended_off_opens_subscriptions_without_consent() -> (
    None
):
    """New behaviour: the person's settings reach the seam every capability opens work through."""
    keyring = FakeKeyring()
    container = build_container(build_settings(), transport=keyring.transport())
    try:
        await container.preferences.aclose()
        fake = FakeSettingsClient({"lucy": {"act_unattended": False, "quiet_hours": "22:30-06:30"}})
        fake.seed("lucy", {"timezone": "Europe/Lisbon"})
        container.preferences = fake
        await container.start()
        session = await container.store.create(ACCOUNT, CreateSession(), "create")
        prepared = await container.prepare_turn(
            PackRequest(
                caller=VerifiedCaller(account_id=ACCOUNT, audience="lucy-api"),
                user_token="verified",
                profile="personal",
                session_id=session["id"],
            ),
            session,
        )

        seam = prepared.pack_context.subscriptions
        assert seam is not None
        assert seam.quiet == QuietHours.of("22:30-06:30", "Europe/Lisbon")
        assert seam.advice().startswith(REPORT_ONLY_ADVICE)
        await seam.open(capability="repos", objective="Merge #42", timeout_seconds=600)
        [row] = await container.subscriptions.open_rows()
        assert row["grant_id"] is None
        assert ("lucy", "personal") in fake.asked
    finally:
        await container.aclose()
