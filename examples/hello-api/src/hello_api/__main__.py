"""CLI entry point."""

from __future__ import annotations

import uvicorn

from hello_api.core.config import load_settings


def main() -> None:
    """Serve the application."""
    settings = load_settings()
    uvicorn.run(
        "hello_api.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
