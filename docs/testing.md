# Testing the hub

```bash
make check      # lint, type, imports, test -- exactly what CI runs
make matrix     # optional: the suite on 3.12 and 3.13
make cov        # an HTML report in htmlcov/
```

`make check` has four gates and does not grow a fifth. Anything else — an eval suite, a
live-model smoke test — is its own verb behind a pytest marker, so the default run stays
deterministic and offline. The conversation regressions are not pytest at all: they are
`lucy eval run` (or `make evals MODEL=...`), held on demand against a running hub, and
only the harness's own logic is tested here, against a fake hub. See
[evals.md](evals.md).

CI gates Python 3.12. Python 3.13 remains declared supported, with the reason it is not
currently gated recorded in [ADR-0008](adr/0008-python-3-12-floor.md). `make matrix` runs
tests on both interpreters; it does not repeat lint, typing or import checks.

## The rules

- **100% branch coverage**, `fail_under = 100`, and **no `pragma: no cover`**. If a line is
  hard to reach, that is usually the design telling you something.
- `filterwarnings = ["error"]`. A warning is a failure.
- mypy `strict = true` over `src`.
- Import contracts are checked, not documented. Routers never import `httpx`,
  `keyring_client` or `settings_client`.

## The shape of a test

Everything runs in-process. `create_app(settings, transport=...)` is the seam: pass
`keyring_client.testing.FakeKeyring().transport()` and the whole hub runs against a fake
keyring that mints real RSA-signed tokens. No network, no live dependency, and no reason
for a test to know a port number.

Fakes follow `settings_client`'s shape — a `Protocol` at the seam, a hand-written `Fake`
that satisfies it, and the real client exercised against the real app in-process. A fake
that drifts from its Protocol is a test that passes while the code is broken.

## Where tests live

| Path | What it covers |
| --- | --- |
| `tests/hub/` | The hub itself. Under coverage. |
| `tests/test_*.py` | The family desk: compose, parity, bootstrap, installers, the reusable workflow, retarget, the connect flow, build secrets. |

The desk's tests are not under `lucy_api` coverage — they exercise scripts and
configuration, not the wheel — but they run in the same command, because a broken
`docker-compose.yml` is as much a defect as a broken route.

`tests/test_setup_scripts.py` runs both installers with fake `uv` and `lucy` commands.
It checks paths containing spaces and apostrophes, forwarded arguments, dry runs and
failure propagation without installing a tool or writing personal configuration. A shell
that is unavailable on the test machine is skipped; Windows uses Git Bash for Bash tests.

## Opt-in suites

```bash
LUCY_TEST_DOCKER=1 uv run pytest tests/test_build_secrets.py -q        # BuildKit secret mounts
LUCY_TEST_FAMILY_IMAGES=1 uv run pytest tests/test_image_runtime.py -q # after make images
```
