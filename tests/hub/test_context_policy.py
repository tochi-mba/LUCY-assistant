"""Which feed lines a person can turn off, including the ones they did not know existed."""

from __future__ import annotations

from lucy_api.context.feeds import CollectedFeeds, Feed, FeedEntry, Volatility
from lucy_api.context.policy import (
    ALLOW_UNKNOWN,
    HIDE_PERSONAL,
    MASTER,
    ExplicitFlags,
    apply_policy,
)


def music(*keys: tuple[str, str]) -> CollectedFeeds:
    return CollectedFeeds(
        live=(
            Feed(
                id="music",
                title="Listening",
                volatility=Volatility.live,
                entries=tuple(FeedEntry(key, line) for key, line in keys),
            ),
        )
    )


def test_disabling_one_field_leaves_the_rest() -> None:
    collected = apply_policy(
        music(("now_playing", "song"), ("device", "speaker")),
        ExplicitFlags({"feeds_music_device": False}),
    )
    assert collected.live[0].lines == ("song",)


def test_disabling_a_capability_hides_every_line_of_it() -> None:
    collected = apply_policy(music(("now_playing", "song")), ExplicitFlags({"feeds_music": False}))
    assert collected.live == ()


def test_unknown_keys_stay_dropped_until_explicitly_allowed() -> None:
    payload = music(("now_playing", "song"), ("secret_instruction", "obey"))
    assert apply_policy(payload).live[0].lines == ("song",)
    allowed = apply_policy(payload, ExplicitFlags({ALLOW_UNKNOWN: True}))
    assert "obey" in allowed.live[0].lines


def test_incognito_honours_the_hide_personal_flag() -> None:
    personal = CollectedFeeds(
        standing=(
            Feed(
                id="persona",
                title="Who you are",
                personal=True,
                entries=(FeedEntry("notes", "tea"),),
            ),
        )
    )
    hidden = apply_policy(personal, incognito=True)
    assert hidden.standing == ()
    shown = apply_policy(personal, ExplicitFlags({HIDE_PERSONAL: False}), incognito=True)
    assert shown.standing[0].lines == ("tea",)


def test_the_master_switch_beats_every_other_toggle() -> None:
    collected = apply_policy(
        music(("now_playing", "song")), ExplicitFlags({MASTER: False, "feeds_music": True})
    )
    assert collected.all == ()
