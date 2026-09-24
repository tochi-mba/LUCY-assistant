#!/usr/bin/env python3
"""Write ``.env.family`` for docker compose: tokens, never printed.

Usage::

    python scripts/genenv.py                               # refuses if .env.family exists
    python scripts/genenv.py --force                       # replace it, keeping the master key
    python scripts/genenv.py --force --rotate-master-key   # replace it all (see below)

The file is gitignored. Values are 32+ random characters. ``KEYRING_SERVICE_TOKENS``
is built from the same strings written to each consumer's real prefixed variable.
``SETTINGS_API_SERVICES`` is the JSON document settings-api's ``services`` field expects.

This script writes a file and prints a count. It never prints a secret.

**`--force` keeps ``KEYRING_MASTER_KEY``.** Every other value is a bearer string whose only
readers are containers started from this same file, so rotating them costs a restart. The
master key encrypts the credentials already stored in keyring's database, and rotating it
makes every one of them unreadable -- a person's Spotify connection, their model keys. On
2026-09-23 the only way to refresh a stale file was to rotate the master key and put the old
one back by hand. Now a refresh keeps it, and rotation is its own, explicit flag.

**Operator-local variables live in** ``scripts/genenv.local.json`` **under** ``"env"``: things
this machine needs and the family does not publish, such as pointing the hub at a model
runtime on the host (``LUCY_MODEL_BASE_URLS``). They are appended as given; a name the
generator writes itself is refused, so an extra can never replace a secret. A variable
present in the old file that neither the generator nor ``"env"`` provides is reported by
name when a refresh drops it, because a hand edit that silently vanishes is how a working
setup stops working after an unrelated regeneration.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

META_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = META_ROOT / ".env.family"
MIN_TOKEN_CHARS = 32
MASTER_KEY_BYTES = 32

# Services that call keyring's /v1/internal. Names are KEYRING_SERVICE_TOKENS keys
# (and the audience those services mint). Variables are the names their Settings classes
# actually read. Operator-local extras belong in scripts/genenv.local.json, not here.
KEYRING_CONSUMERS: tuple[tuple[str, str], ...] = (
    ("lucy-api", "LUCY_KEYRING_SERVICE_TOKEN"),
    ("spotify-api", "SPOTIFY_API_KEYRING_SERVICE_TOKEN"),
    ("web-search-api", "WSA_KEYRING_SERVICE_TOKEN"),
    ("environments-api", "ENVAPI_KEYRING_SERVICE_TOKEN"),
)
# Memory-api is deliberately not here. It verifies keyring's JWTs (`check_service_token`,
# `MEMORY_KEYRING_JWKS_URL`) but never calls keyring's /v1/internal, so it declares no
# `keyring_service_token` field -- and its config refuses unknown MEMORY_* variables, so
# writing one crashes it at startup rather than being ignored. Lucy reaches it with
# `MEMORY_SERVICE_TOKENS` plus a minted person token; `memory-api` is an exchange audience
# below, which is a different thing from being a keyring consumer.

# Exact downstream audiences Lucy may request from Keyring's token exchange. This is an
# allowlist, never inferred from URLs or the service-token map. Adding a sibling does not
# silently give the hub authority to call it for a person.
LUCY_EXCHANGE_AUDIENCES: tuple[str, ...] = (
    "environments-api",
    "memory-api",
    # `persona`, not `persona-api`: the audience persona pins against is not its service name.
    # Keyring mints whatever is asked for, so a wrong entry here is only discovered by the
    # service refusing every token it is sent -- which it did, on every turn, in its own log.
    "persona",
    "settings",
    "spotify-api",
    "user",
    "user.family",
    "user.finance",
    "user.health",
    "user.home",
    "user.work",
    "web-search-api",
)

# settings-api ServiceConfig rows. token_var is the consumer's own prefixed name when
# that service already declares settings_api_token; None means only the grant exists yet.
SETTINGS_GRANTS: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    # The hub reads three namespaces, not one: `lucy` for its own turn policy, and `search`
    # and `spotify` for the sibling defaults `_optional_namespace` folds into a turn -- the
    # person's chosen search backend, result count and playback device. Granted only `lucy`,
    # the other two answered 403 on every turn ever served and `_optional_namespace` swallowed
    # it by design, so those preferences were silently ignored and nothing said so.
    ("lucy-api", "lucy-api", ("lucy", "search", "spotify"), "LUCY_SETTINGS_API_TOKEN"),
    ("user-api", "user", ("user",), None),
    ("persona-api", "persona", ("persona",), None),
    ("spotify-api", "spotify-api", ("spotify",), None),
    ("web-search-api", "web-search-api", ("search",), None),
    ("keyring-api", "keyring", ("keyring",), None),
    ("environments-api", "environments-api", ("environments",), None),
    ("memory-api", "memory-api", ("memory",), None),
)

LOCAL_EXTRAS = META_ROOT / "scripts" / "genenv.local.json"

MASTER_KEY = "KEYRING_MASTER_KEY"

LOCAL_NAME = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")
"""What an operator-local variable may be called: the shape every name here already has."""


class AlreadyExistsError(Exception):
    """The output file already exists, or cannot be written."""


class ExtraConfigError(Exception):
    """scripts/genenv.local.json is present and unusable."""


def load_local_extras(
    path: Path | None,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str, tuple[str, ...], str | None], ...]]:
    """Published lists plus optional gitignored extras. Missing file means none."""
    if path is None or not path.is_file():
        return (), ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ExtraConfigError(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ExtraConfigError(f"{path.name} must be a JSON object")
    return _consumers(data.get("keyring_consumers", []), path.name), _grants(
        data.get("settings_grants", []), path.name
    )


def load_local_env(path: Path | None) -> dict[str, str]:
    """The ``"env"`` object of the local extras file: this machine's own variables."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ExtraConfigError(f"{path.name} is not valid JSON") from exc
    rows = data.get("env", {}) if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        raise ExtraConfigError(f"{path.name}: env must be an object of NAME: value")
    for name, value in rows.items():
        if not LOCAL_NAME.match(name):
            raise ExtraConfigError(f"{path.name}: env name {name!r} is not an UPPER_SNAKE name")
        if not isinstance(value, str):
            raise ExtraConfigError(f"{path.name}: env {name} must be a string")
    return dict(rows)


def read_existing(path: Path) -> dict[str, str]:
    """``KEY=value`` lines from a file this script wrote. Comments and blanks are skipped."""
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        found[name.strip()] = value
    return found


def _consumers(rows: object, source: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(rows, list):
        raise ExtraConfigError(f"{source}: keyring_consumers must be a list")
    out: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ExtraConfigError(f"{source}: keyring_consumers entries must be [name, ENV_VAR]")
        name, variable = row
        if not isinstance(name, str) or not name or not isinstance(variable, str) or not variable:
            raise ExtraConfigError(
                f"{source}: keyring_consumers entries need two non-empty strings"
            )
        out.append((name, variable))
    return tuple(out)


def _grants(rows: object, source: str) -> tuple[tuple[str, str, tuple[str, ...], str | None], ...]:
    if not isinstance(rows, list):
        raise ExtraConfigError(f"{source}: settings_grants must be a list")
    out: list[tuple[str, str, tuple[str, ...], str | None]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            raise ExtraConfigError(
                f"{source}: settings_grants entries must be "
                "[name, audience_prefix, namespaces, token_var_or_null]"
            )
        name, prefix, namespaces, token_var = row
        if not isinstance(name, str) or not name or not isinstance(prefix, str) or not prefix:
            raise ExtraConfigError(f"{source}: settings_grants need a name and audience_prefix")
        if not isinstance(namespaces, list) or not all(
            isinstance(item, str) for item in namespaces
        ):
            raise ExtraConfigError(
                f"{source}: settings_grants namespaces must be a list of strings"
            )
        if token_var is not None and (not isinstance(token_var, str) or not token_var):
            raise ExtraConfigError(f"{source}: token_var must be a non-empty string or null")
        out.append((name, prefix, tuple(namespaces), token_var))
    return tuple(out)


def _merge(
    published: tuple[tuple[str, str], ...], extra: tuple[tuple[str, str], ...], label: str
) -> tuple[tuple[str, str], ...]:
    seen = {row[0] for row in published}
    for row in extra:
        if row[0] in seen:
            raise ExtraConfigError(f"local extra {label} {row[0]!r} duplicates a published service")
        seen.add(row[0])
    return published + extra


def new_token() -> str:
    """URL-safe, longer than the 32-character floor keyring and settings-api enforce."""
    token = secrets.token_urlsafe(32)
    if len(token) < MIN_TOKEN_CHARS:
        msg = "token generator produced a value shorter than the floor"
        raise RuntimeError(msg)
    return token


def new_master_key() -> str:
    """Base64 of 32 random bytes — the shape keyring's ``KEYRING_MASTER_KEY`` requires."""
    return base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode("ascii")


def build_env(
    extras_path: Path | None = LOCAL_EXTRAS, *, master_key: str | None = None
) -> dict[str, str]:
    """One dict, insertion-ordered, ready to serialise as ``KEY=value`` lines.

    `master_key` is the one value carried over rather than generated; see the module
    docstring for why. Operator-local variables from the extras file come last.
    """
    extra_consumers, extra_grants = load_local_extras(extras_path)
    consumers = _merge(KEYRING_CONSUMERS, extra_consumers, "keyring consumer")
    grants_rows = SETTINGS_GRANTS + extra_grants
    seen_grants = {row[0] for row in SETTINGS_GRANTS}
    for row in extra_grants:
        if row[0] in seen_grants:
            raise ExtraConfigError(
                f"local extra settings grant {row[0]!r} duplicates a published service"
            )
        seen_grants.add(row[0])

    env: dict[str, str] = {}

    env[MASTER_KEY] = master_key or new_master_key()
    env["KEYRING_ADMIN_TOKEN"] = new_token()

    keyring_tokens = {name: new_token() for name, _variable in consumers}
    env["KEYRING_SERVICE_TOKENS"] = json.dumps(keyring_tokens, separators=(",", ":"))
    env["KEYRING_EXCHANGE_AUDIENCES"] = json.dumps(
        {"lucy-api": list(LUCY_EXCHANGE_AUDIENCES)}, separators=(",", ":")
    )
    for name, variable in consumers:
        env[variable] = keyring_tokens[name]

    grants: dict[str, dict[str, object]] = {}
    for name, audience_prefix, namespaces, token_var in grants_rows:
        token = new_token()
        grants[name] = {
            "token": token,
            "audience_prefix": audience_prefix,
            "namespaces": list(namespaces),
        }
        if token_var is not None:
            env[token_var] = token
    env["SETTINGS_API_SERVICES"] = json.dumps(grants, separators=(",", ":"))

    # Memory-api's internal map is not Keyring's and not Settings'. Lucy holds the same
    # value as ``lucy-api`` so notes calls prove both the service and the person.
    memory_token = new_token()
    env["MEMORY_SERVICE_TOKENS"] = json.dumps({"lucy-api": memory_token}, separators=(",", ":"))
    env["LUCY_MEMORY_API_TOKEN"] = memory_token

    for name, value in load_local_env(extras_path).items():
        if name in env:
            raise ExtraConfigError(
                f"local env {name} is written by the generator and cannot be replaced"
            )
        env[name] = value
    return env


def render(env: dict[str, str]) -> str:
    lines = [
        "# Generated by scripts/genenv.py for docker compose. Do not commit.",
        "# Re-run with --force to replace. Values are not printed by the generator.",
        "",
    ]
    for key, value in env.items():
        lines.append(f"{key}={value}")
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Written:
    """What a write did, in names and counts only -- never a value."""

    count: int
    kept_master_key: bool = False
    dropped: tuple[str, ...] = ()


def write_env(
    path: Path,
    *,
    force: bool,
    rotate_master_key: bool = False,
    extras_path: Path | None = LOCAL_EXTRAS,
) -> Written:
    """Write the file, keeping the master key unless rotation was asked for by name."""
    if path.exists() and not force:
        raise AlreadyExistsError(f"{path.name} already exists; pass --force to replace it")
    existing = read_existing(path)
    kept = None if rotate_master_key else existing.get(MASTER_KEY) or None
    env = build_env(extras_path, master_key=kept)
    text = render(env)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return Written(
        count=len(env),
        kept_master_key=kept is not None,
        dropped=tuple(sorted(name for name in existing if name not in env)),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write .env.family with random service tokens for docker compose.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing .env.family",
    )
    parser.add_argument(
        "--rotate-master-key",
        action="store_true",
        help=(
            "with --force, generate a new KEYRING_MASTER_KEY too; every credential already "
            "stored in keyring becomes unreadable"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="path to write (default: .env.family in the family root)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = args.output if args.output.is_absolute() else (Path.cwd() / args.output)
    try:
        written = write_env(path, force=args.force, rotate_master_key=args.rotate_master_key)
    except AlreadyExistsError as exc:
        print(f"genenv: {exc}", file=sys.stderr)
        return 1
    except ExtraConfigError as exc:
        print(f"genenv: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"genenv: cannot write {path.name}: {exc.strerror}", file=sys.stderr)
        return 1
    print(f"Wrote {written.count} variables to {path.name}. Re-run with --force to replace it.")
    if written.kept_master_key:
        print(
            f"Kept {MASTER_KEY} from the file it replaced, so stored credentials still decrypt. "
            "Pass --rotate-master-key to replace it as well."
        )
    if written.dropped:
        print(
            f"Not carried over: {', '.join(written.dropped)}. Put this machine's own variables "
            'in scripts/genenv.local.json under "env" and they survive a refresh.'
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
