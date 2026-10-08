"""The verifier's three answers, and configuration that refuses typos."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

import pytest
from keyring_client import AuthenticationError as KeyringRefused
from keyring_client import KeyringUnreachableError as KeyringDown

from lucy_coder.auth import AuthenticationError, KeyringUnreachableError, TokenVerifier
from lucy_coder.config import ENV_PREFIX, Settings, check_for_unknown_env_vars, load_settings
from lucy_coder.runner import Counters, no_tree_kill, taskkill_tree


class Jwks:
    """Enough of a JwksClient for the verifier to construct; never fetched here."""


def a_verifier() -> TokenVerifier:
    class Clock:
        def now(self) -> float:
            return time.time()

    return TokenVerifier(
        jwks=Jwks(),  # type: ignore[arg-type]
        issuer="http://keyring.test",
        audience="coder-api",
        clock=Clock(),  # type: ignore[arg-type]
    )


class Inner:
    def __init__(self, answer: Any) -> None:
        self._answer = answer

    async def verify(self, token: str, *, audience: Any) -> Any:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


async def test_a_good_token_names_the_account_and_the_audience() -> None:
    verifier = a_verifier()

    class Verified:
        account_id = "acct_1"
        audience = "coder-api"

    verifier._verifier = Inner(Verified())
    who = await verifier.verify("token")
    assert (who.account_id, who.audience) == ("acct_1", "coder-api")


async def test_a_refused_token_is_401_shaped_and_keys_down_is_503_shaped() -> None:
    verifier = a_verifier()
    verifier._verifier = Inner(KeyringRefused("no"))
    with pytest.raises(AuthenticationError):
        await verifier.verify("token")
    verifier._verifier = Inner(KeyringDown("down"))
    with pytest.raises(KeyringUnreachableError):
        await verifier.verify("token")


def test_an_unknown_coder_variable_is_refused_at_startup() -> None:
    with pytest.raises(RuntimeError, match="CODER_TYPO"):
        check_for_unknown_env_vars({f"{ENV_PREFIX}TYPO": "x"})
    check_for_unknown_env_vars({f"{ENV_PREFIX}PORT": "8012", "UNRELATED": "y"})


def test_load_settings_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "9999")
    loaded = load_settings()
    assert loaded.port == 9999
    assert loaded.audience == "coder-api"
    assert Settings().max_live_tasks == 2, "the owner's number is the default"
    assert Settings().turn_budget_usd == 3.0, "a first turn's cache build fits under it"


def test_counters_ignore_blocks_that_are_neither_tools_nor_words() -> None:
    counters = Counters()
    counters.saw(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "   "},
                ]
            },
        }
    )
    counters.saw({"type": "assistant", "message": {"content": "not a list"}})
    counters.saw({"type": "rate_limit_event"})
    assert (counters.tool_uses, counters.last_tool, counters.last_text) == (0, "", "")
    assert counters.events == 3


def test_the_posix_sweep_is_a_named_nothing_and_taskkill_is_harmless_off_windows() -> None:
    no_tree_kill(12345)
    if sys.platform == "win32":
        # A real tree: cmd holding a sleeping python. The sweep must take both.
        victim = subprocess.Popen(  # noqa: S602 - a test's own sleeper
            'cmd /c "python -c "import time; time.sleep(60)""', shell=True
        )
        time.sleep(1)
        taskkill_tree(victim.pid)
        assert victim.wait(timeout=10) != 0
    else:
        taskkill_tree(12345)  # the binary is missing and the miss is swallowed
