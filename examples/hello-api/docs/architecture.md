# Architecture

hello-api is a single process. Configuration loads from `HELLO_*` environment
variables. At startup it builds a JWKS client and token verifier; neither fetches
keys until the first authenticated request or readiness check.

```
HTTP → routers → dependencies → auth.verifier → keyring_client
                     ↓
                  core.config
```

There is no database and no outbound credential fetch. Add those only when the
service you are building needs them.
