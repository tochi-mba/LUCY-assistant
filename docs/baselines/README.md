# Eval baselines

Each file here is a `lucy eval baseline`: a known-good run of the regression conversations,
cut down to its measurements. There are no replies, step results, seeds or session ids in
them. Compare a change against one:

```sh
lucy eval run --model clyde:haiku --compare docs/baselines/<file>.json
```

The comparison lists regressions and fixes, then what the run cost next to the baseline:
tokens in, out and cached, model rounds, seconds and turns, per scenario and per model, and
how the fixed prompt every request carries changed, section by section. Lower is better for
all of them, and an optimisation is only finished when it holds every check *and* moves those
numbers.

Re-cut a baseline when a change that moves them is merged, and give it a label that says
what it measured (`--label "after the prompt audit"`). A model's latency and its choice of
plan vary between runs, so take one with `--repeat 3` when a few percent is what you need to
see. [evals.md](../evals.md#baselines-measure-an-optimisation-do-not-guess-it) has the rest.

## Baselines

| File | What it measured | Result |
| --- | --- | --- |
| `clyde-haiku-2026-10-05.json` | `main` at 039a00a on `clyde:haiku`, before the efficiency branch: the fixed prompt at 6,066 tokens. | 7 of 7, 107 of 107 checks; 455,806 tokens in, 11,924 out, 109,041 cache reads over 10 turns; median turn 66s. |
