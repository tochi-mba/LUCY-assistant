"""The sidecar refuses every kind of truncation before calling the model."""

import sys
from types import SimpleNamespace

import pytest
from scripts.laya_server import GuardedRouter, WindowGuard, main


class Tokenizer:
    mask_token = "[MASK]"

    def __call__(self, text, *, add_special_tokens):
        assert not add_special_tokens
        return {"input_ids": text.split()}


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "laya.common",
        SimpleNamespace(render_options=lambda question: list(question["crit"])),
    )
    return SimpleNamespace(
        agent=SimpleNamespace(tok=Tokenizer(), cfg={}),
        states=["short state"],
        max_len=None,
        head_max_len=None,
        questions={"pick": {"type": "choice", "instructions": "Which?", "criteria": ["a", "b"]}},
    )


def test_a_fitting_request_is_passed_unchanged(context):
    WindowGuard().on_predict_start(context)
    assert context.states == ["short state"]
    context.max_len = 100
    context.head_max_len = 80
    WindowGuard().on_predict_start(context)


@pytest.mark.parametrize("part", ["state", "options", "head", "option_budget"])
def test_every_upstream_truncation_path_is_refused(context, part):
    question = context.questions["pick"]
    if part == "state":
        context.states = ["word " * 512]
    elif part == "options":
        question["criteria"] = ["word " * 49, "b"]
    elif part == "head":
        question["instructions"] = "word " * 192
    else:
        context.head_max_len = 16
    with pytest.raises(ValueError, match="no text was truncated"):
        WindowGuard().on_predict_start(context)


def test_nontext_state_is_refused(context):
    context.states = [{}]
    with pytest.raises(TypeError, match="must be text"):
        WindowGuard().on_predict_start(context)


def test_guard_is_always_enabled_on_the_router():
    calls = []

    def predict(state, questions, **options):
        calls.append((state, questions, options))
        return {"answers": {}}

    router = GuardedRouter(SimpleNamespace(predict=predict))
    assert router.predict("s", {}, model="multilingual") == {"answers": {}}
    assert calls[0][2]["hooks_raise"] is True
    assert isinstance(calls[0][2]["hooks"][0], WindowGuard)
    assert calls[0][2]["model"] == "multilingual"


def test_entrypoint_uses_configured_host_and_port(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "laya.serve",
        SimpleNamespace(build_router=lambda: "router", create_app=lambda router: router),
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda app, **kwargs: calls.append((app, kwargs))),
    )
    monkeypatch.setenv("LAYA_HOST", "127.0.0.2")
    monkeypatch.setenv("LAYA_PORT", "8011")
    main()
    assert isinstance(calls[0][0], GuardedRouter)
    assert calls[0][1] == {"host": "127.0.0.2", "port": 8011}
