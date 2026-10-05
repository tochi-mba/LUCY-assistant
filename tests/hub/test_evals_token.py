"""A run outlives its token: the hub client asks ``--token-command`` for a fresh one.

The bug, named: a keyring token lives fifteen minutes, and a conversation with helpers, a
hub restart and seven turns outlives it. The hub then refused every request, and the run
stopped as "the hub refused the token" part way through the conversation it was holding.
"""

from __future__ import annotations

import base64
import json
import sys
from typing import TYPE_CHECKING

import pytest
from eval_fakes import FakeLucy, Play

from lucy_api.cli import evals_hub
from lucy_api.cli.base import OK
from lucy_api.cli.evals_hub import renewed_token
from lucy_api.evals.hub import HubError

if TYPE_CHECKING:
    from pathlib import Path

PRINTS_FRESH = f'"{sys.executable}" -c "print(\'fresh\')"'


def test_a_refused_request_is_sent_again_under_a_fresh_token_asked_for_once() -> None:
    fake = FakeLucy()
    fake.token = "fresh"
    asked: list[str] = []

    def renew() -> str:
        asked.append("asked")
        return "fresh"

    hub = evals_hub.HttpHub(fake.client(), "http://127.0.0.1:8000", "stale", renew=renew)
    assert hub.health()["version"] == fake.version
    assert hub.health()["version"] == fake.version
    assert asked == ["asked"]
    assert [request.headers["Authorization"] for request in fake.requests] == [
        "Bearer stale",
        "Bearer fresh",
        "Bearer fresh",
    ]


def test_a_fresh_token_refused_too_stops_the_run_as_a_refusal() -> None:
    fake = FakeLucy()
    fake.token = "the-right-one"
    hub = evals_hub.HttpHub(fake.client(), "http://127.0.0.1:8000", "stale", renew=lambda: "wrong")
    with pytest.raises(HubError) as refused:
        hub.health()
    assert refused.value.status == 401
    assert refused.value.fatal


def _jwt(claims: object) -> str:
    """A token shaped like keyring's. Its signature is never checked here, only its expiry."""
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJFUzI1NiJ9.{encoded}.signature"


NOW = 1_000_000.0


def test_a_turn_is_never_started_on_a_token_about_to_expire() -> None:
    """The bug, named: a turn started on a token with two minutes left failed part way.

    The hub accepted it at the start, so nothing was refused until the turn was under way;
    then every call to a sibling was, and the scenario failed for the harness's reason.
    """
    fake = FakeLucy()
    session = str(fake.hub().create_session({})["id"])
    ending = _jwt({"exp": NOW + 120})
    fresh = _jwt({"exp": NOW + 900})
    fake.token = fresh
    hub = evals_hub.HttpHub(
        fake.client(), "http://127.0.0.1:8000", ending, renew=lambda: fresh, clock=lambda: NOW
    )
    before = len(fake.requests)
    hub.send_message(session, "hello")
    sent = [request.headers["Authorization"] for request in fake.requests[before:]]
    assert sent == [f"Bearer {fresh}"], "renewed before the turn, not after a refusal"


@pytest.mark.parametrize(
    "token",
    [
        _jwt({"exp": NOW + 900}),
        _jwt({"sub": "no expiry"}),
        _jwt(["not", "an", "object"]),
        "an-opaque-token",
        "not.base64!",
    ],
)
def test_a_token_with_time_left_or_no_expiry_is_kept_for_the_turn(token: str) -> None:
    fake = FakeLucy()
    session = str(fake.hub().create_session({})["id"])
    fake.token = token
    asked: list[str] = []

    def renew() -> str:
        asked.append("asked")
        return "fresh"

    hub = evals_hub.HttpHub(
        fake.client(), "http://127.0.0.1:8000", token, renew=renew, clock=lambda: NOW
    )
    hub.send_message(session, "hello")
    assert asked == []


def test_without_a_token_command_nothing_is_renewed_before_a_turn() -> None:
    fake = FakeLucy()
    session = str(fake.hub().create_session({})["id"])
    ending = _jwt({"exp": NOW + 120})
    fake.token = ending
    hub = evals_hub.HttpHub(fake.client(), "http://127.0.0.1:8000", ending, clock=lambda: NOW)
    hub.send_message(session, "hello")
    assert fake.requests[-1].headers["Authorization"] == f"Bearer {ending}"


def test_the_token_is_whatever_the_command_prints() -> None:
    assert renewed_token(PRINTS_FRESH) == "fresh"


@pytest.mark.parametrize(
    "program",
    [
        "print('secret-token'); raise SystemExit(1)",
        "print('')",
        "print('two words')",
    ],
)
def test_a_command_that_does_not_print_one_token_is_a_refusal_that_never_repeats_it(
    program: str,
) -> None:
    with pytest.raises(HubError) as refused:
        renewed_token(f'"{sys.executable}" -c "{program}"')
    assert refused.value.status == 401
    assert "without printing one token" in str(refused.value)
    assert "secret" not in str(refused.value)
    assert "two" not in str(refused.value)


def test_a_command_that_never_answers_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evals_hub, "TOKEN_COMMAND_SECONDS", 0.5)
    with pytest.raises(HubError, match=r"did not finish in [01]s"):
        renewed_token(f'"{sys.executable}" -c "import time; time.sleep(30)"')


def test_a_run_whose_token_expired_carries_on_under_the_one_the_command_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_evals_cli as cli

    fake = FakeLucy()
    fake.say("Hello?", Play(reply="Hello there."))
    fake.token = "fresh"
    monkeypatch.setattr(cli.httpx, "Client", lambda **_: fake.client())
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "greeting.toml").write_text(cli.SUITE["greeting.toml"], encoding="utf-8")

    code, _out, err = cli.run("--suite", str(folder), "--token-command", PRINTS_FRESH)

    assert code == OK, err
    assert fake.requests[-1].headers["Authorization"] == "Bearer fresh"
