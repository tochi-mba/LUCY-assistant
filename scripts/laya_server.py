"""Serve Laya with a token-window guard: oversized questions abstain, never truncate.

Install laya[serve]==0.3.20 in a separate environment. All model imports are lazy.
The hook checks the exact upstream sequence layout before inference starts.
"""

from __future__ import annotations

import os


class WindowGuard:
    def on_predict_start(self, context):
        from laya.common import render_options

        agent = context.agent
        tokenizer = agent.tok
        max_len = context.max_len or agent.cfg.get("max_len", 512)
        head_max = context.head_max_len or agent.cfg.get("head_max_len", 192)
        for state in context.states:
            if not isinstance(state, str):
                raise TypeError("Decision state must be text")
            state_tokens = tokenizer(
                state.replace(tokenizer.mask_token, " "), add_special_tokens=False
            )["input_ids"]
            for question in context.questions.values():
                normalized = {
                    "t": question["type"],
                    "ins": question["instructions"],
                    "crit": question.get("criteria"),
                }
                options = render_options(normalized)
                lengths = [
                    len(
                        tokenizer(
                            " " + option.replace(tokenizer.mask_token, " "),
                            add_special_tokens=False,
                        )["input_ids"]
                    )
                    for option in options
                ]
                head_tokens = tokenizer(
                    f"{normalized['t']} question: {normalized['ins']}".replace(
                        tokenizer.mask_token, " "
                    ),
                    add_special_tokens=False,
                )["input_ids"]
                option_tokens = sum(length + 1 for length in lengths)
                if (
                    any(length > 48 for length in lengths)
                    or head_max - option_tokens < 16
                    or len(head_tokens) > head_max - option_tokens
                    or len(state_tokens) + len(head_tokens) + option_tokens + 4 > max_len
                ):
                    raise ValueError(
                        "Decision input exceeds the checkpoint window; no text was truncated"
                    )


class GuardedRouter:
    def __init__(self, router):
        self.router = router

    def predict(self, state, questions, *, model=None):
        return self.router.predict(
            state, questions, model=model, hooks=[WindowGuard()], hooks_raise=True
        )


def main():
    import uvicorn
    from laya.serve import build_router, create_app

    app = create_app(GuardedRouter(build_router()))
    uvicorn.run(
        app,
        host=os.environ.get("LAYA_HOST", "127.0.0.1"),
        port=int(os.environ.get("LAYA_PORT", "8010")),
    )


if __name__ == "__main__":
    main()
