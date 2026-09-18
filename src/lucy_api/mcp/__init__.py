"""Lucy as an MCP server: JSON-RPC over ``POST /mcp``, never a third HTTP dialect.

The hub already owns FastAPI, auth, sessions and packs. Mounting the Python MCP SDK's
own Starlette app beside that would be a second request stack with a second opinion
about Origin, 401 and problem documents. The wire types and error codes below are the
2026-07-28 spec; FastAPI is the transport.
"""
