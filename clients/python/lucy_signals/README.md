# lucy-signals

The sibling side of Lucy's jobs contract ([docs/jobs.md](../../../docs/jobs.md)). A service that
watches for something on Lucy's behalf -- CI settling, a review arriving, an export finishing --
ends the subscription by POSTing one signed signal. This package is that POST, done the same way
everywhere:

```python
from lucy_signals import Signal, deliver

await deliver(
    client,                       # your service's httpx.AsyncClient
    url=subscription.signal_url,  # what Lucy gave you when the subscription was opened
    secret=subscription.secret,   # likewise; keep it with the subscription, never log it
    signal=Signal(state="fired", summary="CI on #42 is green", facts={"conclusion": "success"}),
)
```

`deliver` signs the raw body (`X-Lucy-Signature: sha256=<hex HMAC-SHA256>`), retries a refused
connection or a 5xx three times over about two minutes, treats `204` and `409` (already ended)
as delivered, and gives up quietly on anything else, returning what happened. The sweep on Lucy's
side asks again later, so a lost signal costs latency, never the ending.

`verify_signature(secret, header, body)` checks the same scheme, for a service that receives
Lucy's webhooks.

Install it as a tagged git source, the way the family consumes `keyring-client`:

```toml
[tool.uv.sources]
lucy-signals = { git = "https://github.com/tochi-mba/LUCY-assistant", subdirectory = "clients/python/lucy_signals", tag = "lucy-signals-v0.1.0" }
```
