"""The store's two promises are durability and scope, and both are tested against SQLite.

`SqlResultStore` exists because weftai's own store is in-memory: a restart, or a Thursday,
and every `$hits` in a transcript is a dangling pointer. So nothing here is faked. Each
test drives a real database file through the real `SqlWorker`, because half of what is
being claimed -- that a result survives, that expiry is a read-time question, that an
eviction and an insert leave the table in the state the reply describes -- is SQL, and a
fake would only be asked to agree with the implementation it was written from.

Every call goes through the worker on purpose. The Protocol is synchronous and the
connection belongs to one thread, so `await worker.call(lambda _: store.get(...))` is not
ceremony, it is the only way the hub is allowed to reach this object. One test calls a
method straight from the event loop thread to show what happens when somebody forgets.

The codec gets the most attention because it is the part with no type checker behind it.
`StoredResult.data` is `Any`, so nothing but these tests stands between "a collection of
tracks" and "a list of dictionaries that used to be tracks" -- a corruption that would
surface three layers away, as a model being told its own tool returned the wrong shape.
The lossy cases are pinned too: a codec is only honest if what it gives up is written down
and tested, rather than discovered later by whoever hits it.

The scope tests are the security ones. A result carries somebody's search history, so the
question "can session B reach session A's rows?" is asked at every door rather than at
one, because the session predicate is written out again in each method and a copied defence
is the kind that grows a hole the day a method is added.

Where behaviour should match weftai's in-memory store exactly -- eviction order,
replacement, what `SetResult` says -- one test runs the same script through both and
compares, so the contract is checked against its author rather than against this file's
reading of it.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isfunction, signature
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from weftai.errors import RefResolutionError
from weftai.results import create_memory_store
from weftai.results.types import DEFAULT_STORE_LIMITS, ResultStore, StoredResult, StoreLimits

from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore, identifier
from lucy_api.store.results import DEFAULT_LIMITS, MARKER, SqlResultStore, resolve_stored_ref
from lucy_api.store.worker import SqlWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from weftai.results.types import SetResult
    from weftai.schema.types import Collection

ACCOUNT = "acct_owner"
MINUTE = 60_000.0
"""One minute in milliseconds, which is the unit weftai states both limits in."""


class Clock:
    """A clock a test can move. It reads seconds, because that is what the column holds."""

    def __init__(self, now: float = 1_700_000_000.5) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Track(BaseModel):
    """What a music operation returns. The point of the codec is that this comes back."""

    name: str
    artist: str
    duration_ms: int
    released: datetime | None = None


class Hit(BaseModel):
    """A search result. It is also nested inside other values, to prove the codec recurses."""

    url: str
    title: str


def a_collection(
    result_id: str = "hits",
    *,
    items: Sequence[object] = (),
    operation: str = "research.search",
    type_name: str | None = "hits",
    notices: tuple[str, ...] = (),
) -> StoredResult:
    """A collection result shaped the way weftai's executor shapes one: data is the items."""
    frozen = tuple(items)
    return {
        "id": result_id,
        "operation": operation,
        "kind": "collection",
        "type": type_name,
        "data": frozen,
        "items": frozen,
        "count": len(frozen),
        "notices": notices,
        # The executor stamps its step's start time here and the store overwrites it, so a
        # value no store would ever choose makes the overwriting visible.
        "storedAt": 1.0,
    }


def a_value(result_id: str = "answer", *, data: object = "1962") -> StoredResult:
    """A value result: no entities, so no count, no type and nothing to reference."""
    return {
        "id": result_id,
        "operation": "help.docs",
        "kind": "value",
        "type": None,
        "data": data,
        "items": None,
        "count": None,
        "notices": (),
        "storedAt": 1.0,
    }


@dataclass(frozen=True, slots=True)
class Durable:
    """A real store on a real database, plus the two sessions that are not each other's.

    The wrappers exist so a test reads as one sentence about behaviour rather than three
    lines of thread plumbing. They are deliberately thin: each one hands the store's own
    method to the worker and returns what it returned, so nothing is interpreted on the way.
    """

    worker: SqlWorker
    store: SqlResultStore
    clock: Clock
    session: str
    other: str

    async def run[T](self, call: Callable[[], T]) -> T:
        """The only way in: the connection belongs to the worker's thread and to nothing else."""
        return await self.worker.call(lambda _db: call())

    async def store_with(self, limits: StoreLimits) -> SqlResultStore:
        """A second store over the same table, for a deployment that changed its limits."""
        return await self.worker.call(
            lambda db: SqlResultStore(db, limits=limits, clock=self.clock)
        )

    async def keep(self, result: StoredResult, session: str | None = None) -> SetResult:
        target = self.session if session is None else session
        return await self.run(lambda: self.store.set(target, result))

    async def fetch(self, result_id: str, session: str | None = None) -> StoredResult | None:
        target = self.session if session is None else session
        return await self.run(lambda: self.store.get(target, result_id))

    async def listed(self, session: str | None = None) -> list[StoredResult]:
        target = self.session if session is None else session
        return await self.run(lambda: self.store.list(target))

    async def forget(self, result_id: str, session: str | None = None) -> bool:
        target = self.session if session is None else session
        return await self.run(lambda: self.store.delete(target, result_id))

    async def wipe(self, session: str | None = None) -> None:
        target = self.session if session is None else session
        await self.run(lambda: self.store.clear(target))

    async def reference(self, ref: str, session: str | None = None) -> Collection[object] | None:
        target = self.session if session is None else session
        return await self.run(lambda: resolve_stored_ref(self.store, target, ref))

    async def ids(self, session: str | None = None) -> list[str]:
        target = self.session if session is None else session
        return [result["id"] for result in await self.listed(target)]

    async def rows(self) -> list[tuple[str, str]]:
        """Every row in the table, expiry ignored. What a sweep has to be checked against."""
        return await self.worker.call(
            lambda db: [
                (str(row["session_id"]), str(row["id"]))
                for row in db.execute("SELECT session_id,id FROM results ORDER BY rowid")
            ]
        )

    async def rewrite(self, result_id: str, data_json: str) -> None:
        """Edit a stored row in place, standing in for a row an older deployment wrote."""
        await self.worker.call(
            lambda db: db.execute(
                "UPDATE results SET data_json=? WHERE session_id=? AND id=?",
                (data_json, self.session, result_id),
            )
        )


async def a_session(sessions: SessionStore) -> str:
    """A real session row, because `results.session_id` is a foreign key with teeth."""
    created = await sessions.create(ACCOUNT, CreateSession(), identifier("key"))
    return str(created["id"])


@pytest.fixture
async def durable(tmp_path: Path) -> AsyncIterator[Durable]:
    """A file-backed database, always closed: an open WAL keeps `tmp_path` undeletable."""
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    sessions = SessionStore(worker)
    await sessions.initialize()
    clock = Clock()
    store = await worker.call(lambda db: SqlResultStore(db, clock=clock))
    made = Durable(worker, store, clock, await a_session(sessions), await a_session(sessions))
    try:
        yield made
    finally:
        await worker.aclose()


async def test_a_stored_result_comes_back_with_every_field_it_was_handed(
    durable: Durable,
) -> None:
    tracks = (Track(name="Alright", artist="Kendrick Lamar", duration_ms=219_000),)
    outcome = await durable.keep(a_collection("hits", items=tracks, notices=("showing 1 of 12",)))

    read = await durable.fetch("hits")

    assert outcome == {"replaced": False, "evicted": (), "cap": None}
    assert read is not None
    assert read["id"] == "hits"
    assert read["operation"] == "research.search"
    assert read["kind"] == "collection"
    assert read["type"] == "hits"
    assert read["count"] == 1
    assert read["notices"] == ("showing 1 of 12",)
    assert read["data"] == tracks
    assert read["items"] == tracks


async def test_the_store_stamps_its_own_clock_over_the_one_the_caller_sent(
    durable: Durable,
) -> None:
    """Eviction order is only trustworthy if a single clock decides it, so ours wins."""
    await durable.keep(a_collection("hits"))

    read = await durable.fetch("hits")

    assert read is not None
    assert read["storedAt"] == durable.clock.now * 1000
    assert read["storedAt"] != 1.0


async def test_a_collection_of_models_comes_back_as_models_and_not_as_dictionaries(
    durable: Durable,
) -> None:
    """The failure this prevents surfaces far away, as a pack reading `.name` off a dict."""
    tracks = (
        Track(name="Alright", artist="Kendrick Lamar", duration_ms=219_000),
        Track(
            name="Redbone",
            artist="Childish Gambino",
            duration_ms=326_000,
            released=datetime(2016, 11, 17, tzinfo=UTC),
        ),
    )
    await durable.keep(a_collection("hits", items=tracks))

    read = await durable.fetch("hits")

    assert read is not None
    assert all(isinstance(item, Track) for item in read["items"] or ())
    assert read["items"] == tracks
    assert read["data"] == tracks


async def test_a_sequence_keeps_the_difference_between_a_tuple_and_a_list(
    durable: Durable,
) -> None:
    """JSON has one sequence type, and weftai freezes `items` into the other one."""
    await durable.keep(a_value("shapes", data={"frozen": (1, 2), "open": [1, 2]}))

    read = await durable.fetch("shapes")

    assert read is not None
    assert read["data"] == {"frozen": (1, 2), "open": [1, 2]}
    assert isinstance(read["data"]["frozen"], tuple)
    assert isinstance(read["data"]["open"], list)


async def test_a_dictionary_keyed_by_anything_other_than_a_string_keeps_its_keys(
    durable: Durable,
) -> None:
    """A JSON object key is always a string, so `{1: ...}` would come back as `{"1": ...}`."""
    counts = {1: "once", 2: "twice", (3, 4): "a pair"}
    await durable.keep(a_value("counts", data=counts))

    read = await durable.fetch("counts")

    assert read is not None
    assert read["data"] == counts


async def test_a_dictionary_that_happens_to_hold_the_marker_key_is_not_read_as_a_tagged_node(
    durable: Durable,
) -> None:
    """Search results are somebody else's JSON, so the escape has to be real rather than hoped."""
    payload = {MARKER: "tuple", "items": ["not", "a", "tuple"]}
    await durable.keep(a_value("payload", data=payload))

    read = await durable.fetch("payload")

    assert read is not None
    assert read["data"] == payload
    assert isinstance(read["data"]["items"], list)


async def test_nested_models_and_scalars_survive_at_every_depth(durable: Durable) -> None:
    payload = {
        "page": 2,
        "exact": 0.5,
        "empty": None,
        "flag": True,
        "sources": [Hit(url="https://example.test/a", title="A")],
        "pair": (Hit(url="https://example.test/b", title="B"), "note"),
    }
    await durable.keep(a_value("payload", data=payload))

    read = await durable.fetch("payload")

    assert read is not None
    assert read["data"] == payload


async def test_a_value_the_codec_has_no_rule_for_is_kept_as_its_repr_rather_than_failing(
    durable: Durable,
) -> None:
    """A step that succeeded must not fail on the way into storage; it is lossy, and said so."""
    await durable.keep(a_value("path", data=Path("reports") / "q3.md"))

    read = await durable.fetch("path")

    assert read is not None
    assert read["data"] == repr(Path("reports") / "q3.md")


async def test_a_model_whose_class_is_no_longer_importable_reads_back_as_its_dictionary(
    durable: Durable,
) -> None:
    """A durable row outlives the deployment that wrote it, and a rename must not lose it."""

    class Ephemeral(BaseModel):
        """Defined in a function, so its class path names something nobody can import."""

        label: str

    await durable.keep(a_value("gone", data=Ephemeral(label="kept")))

    read = await durable.fetch("gone")

    assert read is not None
    assert read["data"] == {"label": "kept"}


async def test_a_model_whose_module_has_gone_reads_back_as_its_dictionary(
    durable: Durable,
) -> None:
    await durable.keep(a_value("gone", data=Track(name="Alright", artist="K", duration_ms=1)))
    await durable.rewrite(
        "gone",
        '{"$lucy":"model","class":"lucy_api.packs.retired:Track","fields":{"name":"Alright"}}',
    )

    read = await durable.fetch("gone")

    assert read is not None
    assert read["data"] == {"name": "Alright"}


async def test_a_class_path_that_now_names_something_that_is_not_a_model_is_not_trusted(
    durable: Durable,
) -> None:
    """What a stored path resolves to is checked, because the row is older than this process."""
    await durable.keep(a_value("gone", data=Track(name="Alright", artist="K", duration_ms=1)))
    await durable.rewrite(
        "gone",
        '{"$lucy":"model","class":"lucy_api.store.results:SqlResultStore","fields":{"n":1}}',
    )

    read = await durable.fetch("gone")

    assert read is not None
    assert read["data"] == {"n": 1}


async def test_a_value_result_keeps_its_emptiness_rather_than_inventing_entities(
    durable: Durable,
) -> None:
    await durable.keep(a_value("answer"))

    read = await durable.fetch("answer")

    assert read is not None
    assert read["kind"] == "value"
    assert read["items"] is None
    assert read["count"] is None
    assert read["type"] is None
    assert read["data"] == "1962"


async def test_a_grouped_result_keeps_its_rows_and_the_entities_they_came_from(
    durable: Durable,
) -> None:
    """`countBy` returns rows and provenance, and the store is the only thing holding both."""
    hits = (Hit(url="https://example.test/a", title="A"),)
    groups: StoredResult = {
        "id": "byHost",
        "operation": "research.search",
        "kind": "groups",
        "type": "hits",
        "data": ({"key": "example.test", "count": 1},),
        "items": hits,
        "count": 1,
        "notices": (),
        "storedAt": 1.0,
    }

    await durable.keep(groups)
    read = await durable.fetch("byHost")

    assert read is not None
    assert read["kind"] == "groups"
    assert read["data"] == ({"key": "example.test", "count": 1},)
    assert read["items"] == hits


async def test_a_result_from_one_session_is_unreachable_from_another(durable: Durable) -> None:
    """Every method carries the session into the WHERE clause; every method is asked here."""
    await durable.keep(a_collection("hits", items=(Hit(url="https://x.test", title="X"),)))

    assert await durable.fetch("hits", durable.other) is None
    assert await durable.listed(durable.other) == []
    assert await durable.forget("hits", durable.other) is False
    assert await durable.reference("$hits", durable.other) is None

    await durable.wipe(durable.other)

    assert await durable.ids() == ["hits"]


async def test_two_sessions_may_use_the_same_step_id_without_meeting(durable: Durable) -> None:
    """Step ids are the model's words, so `$hits` in two conversations is the normal case."""
    await durable.keep(a_collection("hits", items=(Hit(url="https://a.test", title="A"),)))
    await durable.keep(
        a_collection("hits", items=(Hit(url="https://b.test", title="B"),)), durable.other
    )

    mine = await durable.fetch("hits")
    theirs = await durable.fetch("hits", durable.other)

    assert mine is not None
    assert theirs is not None
    assert mine["items"] == (Hit(url="https://a.test", title="A"),)
    assert theirs["items"] == (Hit(url="https://b.test", title="B"),)


async def test_an_expired_result_is_invisible_to_get_and_to_list(durable: Durable) -> None:
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 10})
    await durable.run(lambda: lean.set(durable.session, a_collection("hits")))

    durable.clock.advance(61)

    assert await durable.run(lambda: lean.get(durable.session, "hits")) is None
    assert await durable.run(lambda: lean.list(durable.session)) == []


async def test_a_result_is_still_live_on_the_last_second_of_its_ttl(durable: Durable) -> None:
    """Expiry is a strict comparison, and a boundary nobody pins is a boundary that moves."""
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 10})
    await durable.run(lambda: lean.set(durable.session, a_collection("hits")))

    durable.clock.advance(59.9)

    assert await durable.run(lambda: lean.get(durable.session, "hits")) is not None


async def test_raising_the_ttl_revives_results_that_had_aged_out(durable: Durable) -> None:
    """Expiry is computed at read time, so a config change is not a generation of dead rows."""
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 10})
    await durable.run(lambda: lean.set(durable.session, a_collection("hits")))
    durable.clock.advance(61)

    patient = await durable.store_with({"ttlMs": 10 * MINUTE, "maxResults": 10})

    assert await durable.run(lambda: patient.get(durable.session, "hits")) is not None


async def test_a_deployment_may_turn_expiry_off_entirely(durable: Durable) -> None:
    """A workspace session lives as long as the work does; an infinite TTL is a real choice."""
    forever = await durable.store_with({"ttlMs": math.inf, "maxResults": 10})
    await durable.run(lambda: forever.set(durable.session, a_collection("hits")))

    durable.clock.advance(365 * 24 * 60 * 60)

    assert await durable.run(lambda: forever.get(durable.session, "hits")) is not None
    assert await durable.rows() == [(durable.session, "hits")]


async def test_expired_rows_are_swept_the_next_time_anything_is_written(
    durable: Durable,
) -> None:
    """Nothing else runs, so a write is the only chance the table gets to lose dead rows."""
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 10})
    await durable.run(lambda: lean.set(durable.session, a_collection("old")))
    await durable.run(lambda: lean.set(durable.other, a_collection("elsewhere")))

    durable.clock.advance(61)
    await durable.run(lambda: lean.set(durable.session, a_collection("fresh")))

    assert await durable.rows() == [(durable.session, "fresh")]


async def test_an_expired_result_does_not_evict_a_live_one(durable: Durable) -> None:
    """The sweep runs before anything is counted, or a dead row would cost a live one."""
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 2})
    await durable.run(lambda: lean.set(durable.session, a_collection("first")))
    durable.clock.advance(61)
    await durable.run(lambda: lean.set(durable.session, a_collection("second")))

    outcome = await durable.run(lambda: lean.set(durable.session, a_collection("third")))

    assert outcome == {"replaced": False, "evicted": (), "cap": None}
    assert await durable.run(lambda: [r["id"] for r in lean.list(durable.session)]) == [
        "second",
        "third",
    ]


async def test_an_expired_result_cannot_be_deleted_because_it_is_already_gone(
    durable: Durable,
) -> None:
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 10})
    await durable.run(lambda: lean.set(durable.session, a_collection("hits")))

    durable.clock.advance(61)

    assert await durable.run(lambda: lean.delete(durable.session, "hits")) is False


async def test_deleting_a_live_result_reports_that_it_removed_one(durable: Durable) -> None:
    await durable.keep(a_collection("hits"))

    assert await durable.forget("hits") is True
    assert await durable.forget("hits") is False
    assert await durable.rows() == []


async def test_clearing_a_session_forgets_what_had_expired_as_well(durable: Durable) -> None:
    """`clear` is somebody deleting a conversation, and half a deletion is not one."""
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 10})
    await durable.run(lambda: lean.set(durable.session, a_collection("old")))
    durable.clock.advance(61)
    await durable.run(lambda: lean.set(durable.session, a_collection("fresh")))

    await durable.run(lambda: lean.clear(durable.session))

    assert await durable.rows() == []


async def test_the_oldest_results_are_evicted_first_and_named_in_the_reply(
    durable: Durable,
) -> None:
    """The ids matter: the runtime turns them into the notice that a `$ref` has gone."""
    lean = await durable.store_with({"ttlMs": 10 * MINUTE, "maxResults": 3})
    for name in ("first", "second", "third"):
        await durable.run(lambda held=name: lean.set(durable.session, a_collection(held)))

    outcome = await durable.run(lambda: lean.set(durable.session, a_collection("fourth")))

    assert outcome == {"replaced": False, "evicted": ("first",), "cap": 3}
    assert await durable.run(lambda: [r["id"] for r in lean.list(durable.session)]) == [
        "second",
        "third",
        "fourth",
    ]


async def test_lowering_the_cap_evicts_as_many_as_it_takes_to_make_room(
    durable: Durable,
) -> None:
    """A deployment may shrink the cap under a session that is already over it."""
    roomy = await durable.store_with({"ttlMs": 10 * MINUTE, "maxResults": 5})
    for name in ("a", "b", "c", "d"):
        await durable.run(lambda held=name: roomy.set(durable.session, a_collection(held)))

    lean = await durable.store_with({"ttlMs": 10 * MINUTE, "maxResults": 2})
    outcome = await durable.run(lambda: lean.set(durable.session, a_collection("e")))

    assert outcome == {"replaced": False, "evicted": ("a", "b", "c"), "cap": 2}
    assert await durable.run(lambda: [r["id"] for r in lean.list(durable.session)]) == ["d", "e"]


async def test_one_session_filling_its_cap_never_evicts_another_sessions_results(
    durable: Durable,
) -> None:
    lean = await durable.store_with({"ttlMs": 10 * MINUTE, "maxResults": 2})
    await durable.run(lambda: lean.set(durable.other, a_collection("theirs")))
    for name in ("a", "b", "c"):
        await durable.run(lambda held=name: lean.set(durable.session, a_collection(held)))

    assert await durable.run(lambda: [r["id"] for r in lean.list(durable.other)]) == ["theirs"]
    assert await durable.run(lambda: [r["id"] for r in lean.list(durable.session)]) == ["b", "c"]


async def test_reusing_a_step_id_replaces_the_result_and_evicts_nothing(
    durable: Durable,
) -> None:
    """A re-run step must not cost the session a different result to make room for itself."""
    lean = await durable.store_with({"ttlMs": 10 * MINUTE, "maxResults": 2})
    await durable.run(lambda: lean.set(durable.session, a_collection("first")))
    await durable.run(lambda: lean.set(durable.session, a_collection("second")))

    outcome = await durable.run(
        lambda: lean.set(
            durable.session,
            a_collection("first", items=(Hit(url="https://new.test", title="New"),)),
        )
    )

    assert outcome == {"replaced": True, "evicted": (), "cap": None}
    read = await durable.run(lambda: lean.get(durable.session, "first"))
    assert read is not None
    assert read["items"] == (Hit(url="https://new.test", title="New"),)
    assert await durable.run(lambda: [r["id"] for r in lean.list(durable.session)]) == [
        "second",
        "first",
    ]


async def test_a_session_is_listed_oldest_first(durable: Durable) -> None:
    for name in ("first", "second", "third"):
        await durable.run(lambda held=name: durable.store.set(durable.session, a_collection(held)))
        durable.clock.advance(1)

    assert await durable.ids() == ["first", "second", "third"]


async def test_the_durable_store_and_weftais_own_agree_about_order_and_eviction(
    durable: Durable,
) -> None:
    """The contract is weftai's, so it is checked against weftai rather than against this file."""
    limits: StoreLimits = {"ttlMs": 10 * MINUTE, "maxResults": 3}
    mine = await durable.store_with(limits)
    theirs = create_memory_store(
        {
            "ttlMs": limits["ttlMs"],
            "maxResults": limits["maxResults"],
            "now": lambda: durable.clock() * 1000,
        }
    )
    script = [
        a_collection("a"),
        a_collection("b"),
        a_collection("c"),
        a_collection("d"),
        a_collection("b", items=(Hit(url="https://again.test", title="Again"),)),
    ]

    ours: list[SetResult] = []
    weftais: list[SetResult] = []
    for written in script:
        ours.append(await durable.run(lambda held=written: mine.set(durable.session, held)))
        weftais.append(theirs.set(durable.session, written))

    assert ours == weftais
    assert await durable.run(lambda: [r["id"] for r in mine.list(durable.session)]) == [
        result["id"] for result in theirs.list(durable.session)
    ]


async def test_a_reference_reads_the_stored_entities_back_without_asking_the_model(
    durable: Durable,
) -> None:
    """The whole point of a plan is that this data never re-enters a context window."""
    hits = tuple(Hit(url=f"https://example.test/{n}", title=str(n)) for n in (1, 2, 3))
    await durable.keep(a_collection("hits", items=hits))

    whole = await durable.reference("$hits")
    one = await durable.reference("$hits[2]")
    several = await durable.reference("$hits[1,3]")

    assert whole is not None
    assert whole.type == "hits"
    assert whole.items == hits
    assert whole.count == 3
    assert one is not None
    assert one.items == (hits[1],)
    assert several is not None
    assert several.items == (hits[0], hits[2])


async def test_a_reference_to_a_result_that_is_not_there_is_absent_rather_than_an_error(
    durable: Durable,
) -> None:
    """Absent is the honest answer for a reference that expired, was evicted, or is not yours."""
    lean = await durable.store_with({"ttlMs": MINUTE, "maxResults": 10})
    await durable.run(lambda: lean.set(durable.session, a_collection("hits")))

    assert await durable.reference("$never") is None

    durable.clock.advance(61)

    assert await durable.run(lambda: resolve_stored_ref(lean, durable.session, "$hits")) is None


async def test_a_reference_that_is_itself_wrong_says_what_to_write_instead(
    durable: Durable,
) -> None:
    """A wrong question is an error, never an empty result, and the message names the fix."""
    hits = (Hit(url="https://example.test/1", title="1"),)
    await durable.keep(a_collection("hits", items=hits))
    await durable.keep(a_value("answer"))

    with pytest.raises(RefResolutionError, match="is not a valid reference"):
        await durable.reference("hits")

    with pytest.raises(RefResolutionError, match="between 1 and 1"):
        await durable.reference("$hits[7]")

    with pytest.raises(RefResolutionError, match="no entities to reference"):
        await durable.reference("$answer")


async def test_a_store_refuses_limits_that_would_make_it_useless(durable: Durable) -> None:
    """A zero anywhere here silently throws away every result, so it fails at construction."""
    with pytest.raises(ValueError, match="positive number of milliseconds"):
        await durable.store_with({"ttlMs": 0, "maxResults": 10})

    with pytest.raises(ValueError, match="at least one result"):
        await durable.store_with({"ttlMs": MINUTE, "maxResults": 0})


async def test_the_hub_keeps_results_far_longer_than_one_chat_turn(durable: Durable) -> None:
    """The defaults are the decision this module exists to make, so they are pinned."""
    assert DEFAULT_LIMITS["ttlMs"] == 7 * 24 * 60 * 60 * 1000
    assert DEFAULT_LIMITS["ttlMs"] > DEFAULT_STORE_LIMITS["ttlMs"]
    assert DEFAULT_LIMITS["maxResults"] > DEFAULT_STORE_LIMITS["maxResults"]

    await durable.keep(a_collection("hits"))
    durable.clock.advance(6 * 24 * 60 * 60)

    assert await durable.fetch("hits") is not None


async def test_the_store_answers_weftais_protocol_with_the_argument_names_weftai_uses(
    durable: Durable,
) -> None:
    """Read off the Protocol rather than listed here: a copied list goes quietly out of date."""
    declared = {
        name: value
        for name, value in vars(ResultStore).items()
        if isfunction(value) and not name.startswith("_")
    }

    assert set(declared) == {"get", "set", "list", "delete", "clear"}
    for name, value in declared.items():
        mine = signature(getattr(type(durable.store), name))
        assert list(mine.parameters) == list(signature(value).parameters), name


async def test_the_store_refuses_to_be_used_from_the_thread_it_does_not_belong_to(
    durable: Durable,
) -> None:
    """The rule is stated in prose, and this is what enforces it when somebody forgets."""
    with pytest.raises(sqlite3.ProgrammingError, match="same thread"):
        durable.store.get(durable.session, "hits")


async def test_a_result_for_a_session_that_does_not_exist_is_refused_by_the_database(
    durable: Durable,
) -> None:
    """Results belong to conversations; an orphan row would outlive the thing it describes."""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await durable.keep(a_collection("hits"), "ses_never_created")


async def test_deleting_a_session_takes_its_results_with_it(durable: Durable) -> None:
    """The cascade is the schema's promise; it only holds if the rows are really children."""
    await durable.keep(a_collection("hits"))

    await durable.worker.call(
        lambda db: db.execute("DELETE FROM sessions WHERE id=?", (durable.session,))
    )

    assert await durable.rows() == []


async def test_a_store_built_without_a_clock_reads_the_wall_clock(durable: Durable) -> None:
    """The default construction is the one production takes, so the fixture does not hide it."""
    default = await durable.worker.call(SqlResultStore)
    await durable.run(lambda: default.set(durable.session, a_collection("hits")))

    read = await durable.run(lambda: default.get(durable.session, "hits"))

    assert read is not None
    assert read["storedAt"] == pytest.approx(time.time() * 1000, abs=5_000)
