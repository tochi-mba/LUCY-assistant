"""Safe failures shared by domain services and their HTTP adapters."""

from __future__ import annotations


class LucyError(Exception):
    """An authored, value-free explanation with a stable machine code."""

    def __init__(self, code: str, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status


def absent() -> LucyError:
    """Foreign and nonexistent resources deliberately produce the same response."""
    return LucyError("not-found", "The resource was not found.", 404)


def conflict(detail: str) -> LucyError:
    return LucyError("conflict", detail, 409)


def settings_unavailable(detail: str) -> LucyError:
    """A refuse key could not be confirmed, so this turn must not guess."""
    return LucyError("settings-unavailable", detail, 503)
