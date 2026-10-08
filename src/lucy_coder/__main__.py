"""Server entry point: ``python -m lucy_coder`` from the repository root."""

from __future__ import annotations

import uvicorn
from keyring_client import JwksClient, SystemClock

from lucy_coder.app import create_app
from lucy_coder.auth import TokenVerifier
from lucy_coder.config import load_settings
from lucy_coder.runner import ClaudeRunner
from lucy_coder.service import CoderService
from lucy_coder.tasks import TaskStore


def main() -> None:
    settings = load_settings()
    clock = SystemClock()
    verifier = TokenVerifier(
        jwks=JwksClient(
            url=settings.keyring_jwks_url,
            clock=clock,
            cache_seconds=settings.jwks_cache_seconds,
            min_refetch_seconds=settings.jwks_min_refetch_seconds,
            timeout_seconds=settings.keyring_timeout_seconds,
        ),
        issuer=settings.keyring_issuer,
        audience=settings.audience,
        clock=clock,
    )
    service = CoderService(
        TaskStore(settings.var_dir),
        ClaudeRunner(
            settings.claude_command,
            budget_usd=settings.turn_budget_usd,
            timeout_seconds=settings.turn_timeout_seconds,
        ),
        max_live=settings.max_live_tasks,
    )
    uvicorn.run(
        create_app(settings, service, verifier),
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
