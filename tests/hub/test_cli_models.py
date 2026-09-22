"""`lucy models` renders the hub's report; `lucy models connect` gives the hub a key.

What is pinned: the client never invents a provider list, the three sections read in the
order a person wants them, a key is typed at a prompt and lands in the hub's `.env` without
disturbing the lines around it, and every refusal names what to do instead.
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import httpx
import pytest

from lucy_api.cli.base import OK, REFUSED, TOKEN_VAR, USAGE
from lucy_api.cli.main import main as cli_main
from lucy_api.cli.models import KEYS_VARIABLE

REAL_CLIENT = httpx.Client


class FakeStream(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def isolated_cli_environment(tmp_path, monkeypatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("LUCY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LUCY_CONFIG", str(tmp_path / "config.toml"))
    # Pointed at a directory that is not a family checkout, so the command's fallback to
    # the installed tree can never find the real one and write a test key into its `.env`.
    monkeypatch.setenv("LUCY_FAMILY_ROOT", str(tmp_path / "nowhere"))
    monkeypatch.chdir(tmp_path)


def run(argv, *, environ=None, stdin="", tty=False):
    out, err = FakeStream(tty=tty), FakeStream(tty=tty)
    env = {
        "LUCY_CONFIG": os.environ["LUCY_CONFIG"],
        "LUCY_FAMILY_ROOT": os.environ["LUCY_FAMILY_ROOT"],
        **(environ or {}),
    }
    code = cli_main(argv, out=out, err=err, in_=FakeStream(stdin, tty=tty), environ=env)
    return code, out.getvalue(), err.getvalue()


def row(provider: str, section: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": provider,
        "title": provider.title(),
        "section": section,
        "dialect": "openai-chat",
        "models": ["m-1"],
        "detail": {"ready": "answered", "available": "configured; not checked yet"}.get(
            section, "no key configured"
        ),
        "local": False,
        "note": "",
    }
    if section == "unavailable":
        body["setup"] = {
            "command": f"lucy models connect {provider}",
            "console_url": f"https://console.{provider}.invalid",
            "setting": KEYS_VARIABLE,
            "instructions": "Create a key and supply it.",
        }
    body.update(overrides)
    return body


def report(**sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ready": [], "available": [], "unavailable": [], **sections}


def listing_hub(body: dict[str, Any], *, status: int = 200, text: str | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            if text is not None:
                return httpx.Response(status, text=text)
            return httpx.Response(status, json=body)
        raise AssertionError(f"{request.method} {request.url.path}")

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


@pytest.fixture
def patched(monkeypatch):
    def install(handler):
        monkeypatch.setattr(
            httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(handler))
        )

    return install


@pytest.fixture
def family_root(tmp_path, monkeypatch):
    """A checkout the command can find, with the two markers `find_family_root` looks for."""
    root = tmp_path / "family"
    root.mkdir()
    (root / "family-app.json").write_text("{}")
    (root / "repos.txt").write_text("")
    monkeypatch.setenv("LUCY_FAMILY_ROOT", str(root))
    return root


# --------------------------------------------------------------------------------------
# lucy models
# --------------------------------------------------------------------------------------


def test_the_three_sections_read_in_the_order_a_person_wants_them(patched) -> None:
    patched(
        listing_hub(
            report(
                ready=[row("anthropic", "ready")],
                available=[row("groq", "available")],
                unavailable=[row("deepseek", "unavailable")],
            )
        )
    )
    code, out, _ = run(["models"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert out.index("ready -- checked") < out.index("available -- configured")
    assert out.index("available -- configured") < out.index("unavailable -- and what")
    assert "anthropic" in out
    assert "(m-1)" in out
    assert "run: lucy models connect deepseek" in out
    assert "Nothing is ready" not in out


def test_nothing_ready_says_what_to_run(patched) -> None:
    patched(listing_hub(report(unavailable=[row("deepseek", "unavailable")])))
    code, out, _ = run(["models"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert "Nothing is ready. Run `lucy models connect <provider>` to add one." in out


def test_a_provider_that_cannot_be_connected_shows_no_command(patched) -> None:
    vertex = row("vertex", "unavailable", detail="cannot be used by this hub yet")
    vertex["setup"]["command"] = ""
    patched(listing_hub(report(unavailable=[vertex])))
    code, out, _ = run(["models"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert "run:" not in out.split("vertex", 1)[1].splitlines()[0]


def test_check_asks_the_hub_to_prove_the_keys(patched) -> None:
    handler = listing_hub(report(ready=[row("anthropic", "ready")]))
    patched(handler)
    code, _, _ = run(["models", "--check"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert handler.seen[-1].url.query == b"check=true"


def test_json_emits_the_hubs_report_untouched(patched) -> None:
    body = report(ready=[row("anthropic", "ready")])
    patched(listing_hub(body))
    code, out, _ = run(["models", "--json"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert json.loads(out) == body


def test_it_needs_a_token(patched) -> None:
    patched(listing_hub(report()))
    code, _, err = run(["models"])
    assert code == REFUSED
    assert "not signed in" in err


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "the hub refused the token"),
        (404, "does not list models yet"),
        (503, "cannot list models right now"),
    ],
)
def test_each_hub_refusal_is_a_sentence_with_a_next_step(patched, status, expected) -> None:
    patched(listing_hub({}, status=status))
    code, _, err = run(["models"], environ={TOKEN_VAR: "t"})
    assert code == REFUSED
    assert expected in err


@pytest.mark.parametrize("text", ["not json", '{"ready": "nope"}'])
def test_an_unreadable_listing_is_refused_rather_than_rendered(patched, text) -> None:
    patched(listing_hub({}, text=text))
    code, _, err = run(["models"], environ={TOKEN_VAR: "t"})
    assert code == REFUSED
    assert "unreadable model listing" in err


# --------------------------------------------------------------------------------------
# lucy models connect <provider>
# --------------------------------------------------------------------------------------


def test_connecting_prompts_for_the_key_and_writes_it_into_the_hubs_env(
    patched, family_root
) -> None:
    patched(listing_hub(report(unavailable=[row("deepseek", "unavailable")])))
    env_file = family_root / ".env"
    env_file.write_text("LUCY_PORT=8000\n# keep me\n")

    code, out, err = run(
        ["models", "connect", "deepseek"],
        environ={TOKEN_VAR: "t"},
        stdin="sk-typed\n",
        tty=True,
    )

    assert code == OK, err
    assert "deepseek API key (create one at https://console.deepseek.invalid):" in err
    assert "Restart the hub" in err
    assert out == ""
    lines = env_file.read_text().splitlines()
    assert lines[:2] == ["LUCY_PORT=8000", "# keep me"], "the other lines are untouched"
    assert lines[2] == "LUCY_MODEL_KEYS='" + json.dumps({"deepseek": "sk-typed"}) + "'"
    assert "sk-typed" not in out


def test_a_second_key_is_merged_with_the_first_not_written_over_it(patched, family_root) -> None:
    patched(listing_hub(report(unavailable=[row("groq", "unavailable")])))
    (family_root / ".env").write_text(f"{KEYS_VARIABLE}='" + json.dumps({"deepseek": "a"}) + "'\n")

    code, _, _ = run(["models", "connect", "groq"], environ={TOKEN_VAR: "t"}, stdin="b\n", tty=True)

    assert code == OK
    saved = (family_root / ".env").read_text().splitlines()[0]
    assert saved == f"{KEYS_VARIABLE}='" + json.dumps({"deepseek": "a", "groq": "b"}) + "'"


def test_an_unreadable_existing_value_is_replaced_rather_than_crashed_on(
    patched, family_root
) -> None:
    patched(listing_hub(report(unavailable=[row("groq", "unavailable")])))
    (family_root / ".env").write_text(f'{KEYS_VARIABLE}="not json"\n')

    code, _, _ = run(["models", "connect", "groq"], environ={TOKEN_VAR: "t"}, stdin="b\n", tty=True)

    assert code == OK
    assert json.loads((family_root / ".env").read_text().split("=", 1)[1].strip("'\n")) == {
        "groq": "b"
    }


def test_a_local_runtime_is_switched_on_without_a_prompt(patched, family_root) -> None:
    ollama = row("ollama", "unavailable", local=True, detail="not switched on")
    patched(listing_hub(report(unavailable=[ollama])))

    code, _, err = run(["models", "connect", "ollama"], environ={TOKEN_VAR: "t"})

    assert code == OK
    assert "ollama switched on" in err
    assert "API key" not in err
    assert json.loads((family_root / ".env").read_text().split("=", 1)[1].strip("'\n")) == {
        "ollama": "local"
    }


def test_a_key_is_never_taken_from_a_flag_or_a_pipe(patched, family_root) -> None:
    """Without a terminal there is nobody to type it, and a flag would land in history."""
    patched(listing_hub(report(unavailable=[row("deepseek", "unavailable")])))
    code, _, err = run(
        ["models", "connect", "deepseek"], environ={TOKEN_VAR: "t"}, stdin="sk-piped\n"
    )
    assert code == USAGE
    assert "needs a terminal" in err
    assert "a key is never a flag" in err
    assert not (family_root / ".env").exists()


def test_an_empty_answer_changes_nothing(patched, family_root) -> None:
    patched(listing_hub(report(unavailable=[row("deepseek", "unavailable")])))
    code, _, err = run(
        ["models", "connect", "deepseek"], environ={TOKEN_VAR: "t"}, stdin="\n", tty=True
    )
    assert code == REFUSED
    assert "nothing was changed" in err
    assert not (family_root / ".env").exists()


def test_a_provider_that_is_already_ready_is_left_alone(patched, family_root) -> None:
    patched(listing_hub(report(ready=[row("anthropic", "ready")])))
    code, _, err = run(["models", "connect", "anthropic"], environ={TOKEN_VAR: "t"}, tty=True)
    assert code == OK
    assert "already connected" in err
    assert not (family_root / ".env").exists()


def test_a_provider_the_hub_cannot_use_is_refused_with_its_reason(patched) -> None:
    vertex = row("vertex", "unavailable")
    vertex["setup"] = {"command": "", "instructions": "Needs an OAuth token."}
    patched(listing_hub(report(unavailable=[vertex])))
    code, _, err = run(["models", "connect", "vertex"], environ={TOKEN_VAR: "t"}, tty=True)
    assert code == REFUSED
    assert "cannot be connected from here" in err
    assert "Needs an OAuth token" in err


def test_an_unknown_provider_points_at_the_listing(patched) -> None:
    patched(listing_hub(report()))
    code, _, err = run(["models", "connect", "gemeni"], environ={TOKEN_VAR: "t"}, tty=True)
    assert code == USAGE
    assert "no model provider called 'gemeni'" in err


def test_without_a_checkout_to_write_to_the_variable_is_named_instead(patched) -> None:
    patched(listing_hub(report(unavailable=[row("deepseek", "unavailable")])))
    code, _, err = run(["models", "connect", "deepseek"], environ={TOKEN_VAR: "t"}, tty=True)
    assert code == REFUSED
    assert "cannot find the hub's checkout" in err
    assert KEYS_VARIABLE in err


def test_a_write_that_fails_is_a_sentence_not_a_traceback(
    patched, family_root, monkeypatch
) -> None:
    patched(listing_hub(report(unavailable=[row("deepseek", "unavailable")])))

    def refuse(self, target):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.replace", refuse)
    code, _, err = run(
        ["models", "connect", "deepseek"], environ={TOKEN_VAR: "t"}, stdin="k\n", tty=True
    )
    assert code == REFUSED
    assert "cannot write" in err
    assert not list(family_root.glob(".env.*.tmp")), "the temporary file was cleaned up"


def test_an_unquoted_existing_value_and_a_bare_line_are_both_read_correctly(
    patched, family_root
) -> None:
    """Somebody who edited `.env` by hand may have left the JSON bare and a stray line."""
    patched(
        listing_hub(
            report(ready=[row("anthropic", "ready")], unavailable=[row("groq", "unavailable")])
        )
    )
    (family_root / ".env").write_text(
        "just a bare line\n" + KEYS_VARIABLE + "=" + json.dumps({"deepseek": "a"}) + "\n"
    )

    code, _, _ = run(["models", "connect", "groq"], environ={TOKEN_VAR: "t"}, stdin="b\n", tty=True)

    assert code == OK
    lines = (family_root / ".env").read_text().splitlines()
    assert lines[0] == "just a bare line"
    assert lines[1] == f"{KEYS_VARIABLE}='" + json.dumps({"deepseek": "a", "groq": "b"}) + "'"


def test_a_temporary_file_that_cannot_be_created_is_a_sentence_too(
    patched, family_root, monkeypatch
) -> None:
    patched(listing_hub(report(unavailable=[row("deepseek", "unavailable")])))

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("tempfile.NamedTemporaryFile", refuse)
    code, _, err = run(
        ["models", "connect", "deepseek"], environ={TOKEN_VAR: "t"}, stdin="k\n", tty=True
    )
    assert code == REFUSED
    assert "cannot write" in err
    assert "No space left" in err


def test_an_existing_value_that_is_json_but_not_an_object_is_replaced(patched, family_root) -> None:
    patched(listing_hub(report(unavailable=[row("groq", "unavailable")])))
    (family_root / ".env").write_text(f"{KEYS_VARIABLE}='[1, 2]'\n")

    code, _, _ = run(["models", "connect", "groq"], environ={TOKEN_VAR: "t"}, stdin="b\n", tty=True)

    assert code == OK
    assert json.loads((family_root / ".env").read_text().split("=", 1)[1].strip("'\n")) == {
        "groq": "b"
    }
