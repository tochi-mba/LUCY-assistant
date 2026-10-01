"""What a person chose in a sibling's settings becomes the default a model's omission falls to.

Pinned per namespace: a valid value is passed through under the capability's name, an absent
namespace supplies nothing, and a value of the wrong kind is ignored rather than guessed at.
"""

from __future__ import annotations

from typing import Any

import pytest

from lucy_api.settings.defaults import (
    MUSIC_NAMESPACE,
    REPOS_NAMESPACE,
    SEARCH_NAMESPACE,
    pack_defaults,
)


def namespace(**values: Any) -> dict[str, Any]:
    return values


def test_no_namespace_resolved_supplies_nothing() -> None:
    assert pack_defaults({}) == {}
    assert pack_defaults({SEARCH_NAMESPACE: None, REPOS_NAMESPACE: None}) == {}


def test_each_namespace_supplies_its_capabilitys_defaults() -> None:
    defaults = pack_defaults(
        {
            SEARCH_NAMESPACE: namespace(default_result_count=50, search_backend="brave"),
            MUSIC_NAMESPACE: namespace(default_device="kitchen"),
            REPOS_NAMESPACE: namespace(default_owner="acme", default_visibility="internal"),
        }
    )
    assert defaults == {
        "research.limit": 20,
        "research.backend": "brave",
        "music.device_id": "kitchen",
        "repos.owner": "acme",
        "repos.visibility": "internal",
    }


def test_search_falls_back_to_its_own_defaults_when_a_key_is_unset() -> None:
    assert pack_defaults({SEARCH_NAMESPACE: namespace()}) == {
        "research.limit": 8,
        "research.backend": "google",
    }


@pytest.mark.parametrize(
    ("resolved", "absent"),
    [
        ({SEARCH_NAMESPACE: namespace(default_result_count=True, search_backend="")}, "research"),
        ({MUSIC_NAMESPACE: namespace(default_device="")}, "music"),
        ({REPOS_NAMESPACE: namespace(default_owner=3, default_visibility="secret")}, "repos"),
        ({REPOS_NAMESPACE: namespace()}, "repos"),
    ],
    ids=["search-wrong-kinds", "music-empty", "repos-wrong-kinds", "repos-unset"],
)
def test_a_value_of_the_wrong_kind_is_ignored(resolved: dict[str, Any], absent: str) -> None:
    assert not any(key.startswith(f"{absent}.") for key in pack_defaults(resolved))
