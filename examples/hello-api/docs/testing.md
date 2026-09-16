# Testing

`make check` runs ruff, mypy, import-linter, and pytest with 100% branch coverage.
Tests mint tokens with `keyring_client.testing` over an `httpx.MockTransport`; they
do not need a live keyring.
