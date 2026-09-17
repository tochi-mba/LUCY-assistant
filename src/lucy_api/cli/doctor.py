"""`lucy doctor` -- why isn't this working.

Every check is independent and none of them raise: a machine with no network still gets a
full report of everything that *can* be answered locally, because a diagnostic that stops
at the first problem makes you run it once per problem.

Three levels, and the distinction is the point. **fail** is something that will stop Lucy
working and has a fix. **warn** is something that will surprise you later -- an environment
variable quietly overriding the file you just wrote, a key in the config nothing reads.
**ok** is stated rather than omitted, because a person reading a diagnostic wants to see
what was checked, not just what broke.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from lucy_api.cli import config as config_module

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.cli.config import Config

MINIMUM_PYTHON = (3, 12)
HTTP_OK = 200

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """One answered question, and -- when the answer is bad -- what to do about it."""

    name: str
    level: str
    detail: str
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.level == FAIL


IDENTITY_FAILURES = {
    HTTPStatus.UNAUTHORIZED: Check(
        "identity",
        FAIL,
        "the hub refused the token (401)",
        "the token is expired or its audience is not lucy-api; run lucy setup",
    ),
    HTTPStatus.FORBIDDEN: Check(
        "identity",
        FAIL,
        "the hub denied access (403)",
        "check this account's permissions with the hub administrator",
    ),
    HTTPStatus.TOO_MANY_REQUESTS: Check(
        "identity",
        WARN,
        "the hub is rate limiting identity checks (429)",
        "try again later",
    ),
}


def _python_check() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] >= MINIMUM_PYTHON:
        return Check("python", OK, version)
    wanted = ".".join(str(part) for part in MINIMUM_PYTHON)
    return Check(
        "python",
        FAIL,
        f"{version}, and Lucy needs {wanted} or newer",
        f"uv python install {wanted}, then reinstall: uv tool install --force --editable .",
    )


def _command_check(name: str, *, level_when_missing: str, fix: str) -> Check:
    found = shutil.which(name)
    if found:
        return Check(name, OK, found)
    return Check(name, level_when_missing, "not on PATH", fix)


def _config_checks(config: Config, environ: Mapping[str, str]) -> list[Check]:
    """The file, and then the things about it that surprise people.

    An environment variable silently winning over a file somebody just wrote is the single
    most confusing state this command can be in, so it is called out by name rather than
    left for them to discover.
    """
    checks: list[Check] = []
    if config.exists:
        checks.append(Check("config", OK, str(config.path)))
    else:
        checks.append(Check("config", WARN, f"no file at {config.path}", "lucy setup"))
    if config.unknown:
        keys = ", ".join(config.unknown)
        checks.append(
            Check("config keys", WARN, f"nothing reads {keys}", "remove them, or fix the spelling")
        )
    for variable, key in (("LUCY_URL", "url"), ("LUCY_TOKEN", "token")):
        if environ.get(variable, "").strip() and config.get(key):
            checks.append(
                Check(
                    variable,
                    WARN,
                    f"set, and it wins over {key} in the config file",
                    f"unset {variable} to use the configured value",
                )
            )
    return checks


def _token_check(token: str) -> Check:
    if not token:
        return Check("token", WARN, "not signed in", "lucy setup, or set LUCY_TOKEN")
    jwt_segments = 3
    if token.count(".") != jwt_segments - 1:
        return Check(
            "token",
            WARN,
            "present, but not shaped like a keyring token",
            "keyring issues a three-part JWT; check you pasted the whole thing",
        )
    return Check("token", OK, config_module.redact(token))


def environment_checks(*, config: Config, environ: Mapping[str, str], token: str) -> list[Check]:
    """Everything answerable without a network."""
    checks = [_python_check()]
    checks.append(
        _command_check(
            "lucy",
            level_when_missing=WARN,
            fix="uv tool install --editable . && uv tool update-shell",
        )
    )
    checks.append(
        _command_check("uv", level_when_missing=WARN, fix="https://docs.astral.sh/uv/ to install")
    )
    checks.append(
        _command_check(
            "docker", level_when_missing=WARN, fix="only needed to run the whole family locally"
        )
    )
    checks.extend(_config_checks(config, environ))
    checks.append(_token_check(token))
    return checks


def _identity_check(response: Any) -> Check:
    """A failed authentication dependency never tells somebody to sign in again."""
    if response is None:
        return Check("identity", WARN, "not checked without a token")
    status = response.status_code
    if status == HTTP_OK:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("account_id"), str):
            return Check("identity", OK, payload["account_id"])
        return Check(
            "identity", FAIL, "the hub returned an invalid identity response", "check LUCY_URL"
        )
    if status in IDENTITY_FAILURES:
        return IDENTITY_FAILURES[status]
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return Check(
            "identity",
            FAIL,
            f"identity verification is unavailable ({status})",
            "restore the hub's authentication dependency, then retry with the same token",
        )
    return Check(
        "identity",
        FAIL,
        f"the identity endpoint returned an unexpected status ({status})",
        "check LUCY_URL and that the hub supports this client",
    )


def hub_checks(url: str, responses: Mapping[str, Any] | None, error: str | None) -> list[Check]:
    """What the hub said, or why it said nothing.

    `responses` is `None` when the hub could not be reached at all. That is one failure with
    one fix, not three, so it is reported once instead of failing every dependent check.
    """
    if responses is None:
        return [
            Check(
                "hub",
                FAIL,
                error or f"cannot reach {url}",
                "lucy serve, or make up, or set LUCY_URL to where it runs",
            )
        ]

    checks = [Check("hub", OK, url)]
    ready = responses.get("ready")
    if ready is not None and ready.status_code == HTTP_OK:
        checks.append(Check("ready", OK, "every dependency is usable"))
    else:
        checks.append(
            Check("ready", FAIL, "a dependency is unusable", "the checks below say which")
        )
    states = responses.get("checks", {})
    if isinstance(states, Mapping):
        for name, state in sorted(states.items()):
            level = OK if state == "ok" else FAIL
            checks.append(
                Check(f"  {name}", level, str(state), "" if level == OK else f"start {name}")
            )
    else:
        checks.append(Check("dependencies", FAIL, "the hub returned invalid dependency checks"))
    checks.append(_identity_check(responses.get("me")))
    return checks


def worst(checks: Sequence[Check]) -> str:
    """One word for the whole report, so a script can branch on it."""
    if any(check.level == FAIL for check in checks):
        return FAIL
    if any(check.level == WARN for check in checks):
        return WARN
    return OK
