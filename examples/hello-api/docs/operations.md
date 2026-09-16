# Operations

Listen on `HELLO_HOST`:`HELLO_PORT` (default `127.0.0.1:8090`). Point container
healthchecks at `/healthy` and load balancers at `/ready`.

Required for real tokens: `HELLO_KEYRING_JWKS_URL`, `HELLO_KEYRING_ISSUER`, and
`HELLO_AUDIENCE` matching the audience you ask keyring to mint.
