# Fronting hello-api with an MCP server

Nothing MCP-specific is implemented. hello-api is the family's teaching service:
liveness, readiness, and one authenticated route that echoes the verified
account id. It holds no personal data and no third-party credentials.

When a wrapper is added, `operation_id`s become tool names. The authenticated
route today is `GET /v1/whoami`; a real family service sets that id explicitly
so it stays stable. hello-api currently lets FastAPI generate it — that is
acceptable here because the example is not a product surface, and it is the
thing a copy-paste would need to change first.

There is nothing in this service to render as a reported claim. The only
identity it ever learns is the `sub` of a verified Bearer token, and that is
already a fact rather than a stored memory.
