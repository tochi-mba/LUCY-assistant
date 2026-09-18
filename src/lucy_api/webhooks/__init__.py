"""Long-run push: a signal, never a payload.

A webhook exists so a client that cannot hold an SSE connection still learns that a turn
ended, parked, or needs a credential. The body names the session, the turn and the status.
The transcript stays behind `GET /v1/turns/{id}`. That split is the whole product: a third
party log of Lucy's events must not become a third party copy of the conversation.
"""

from lucy_api.webhooks.service import Webhooks, httpx_deliver

__all__ = ["Webhooks", "httpx_deliver"]
