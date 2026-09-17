"""Where `lucy` keeps what it was told, so a person types it once.

A globally installed command is used from any directory, which means it cannot read the
repository's `.env`. Exporting two variables in every shell is not a setup flow, so the
answer people expect -- and that `gh`, `aws` and `kubectl` all give them -- is a small file
in the user's own configuration directory.

**A flag wins over the environment, and the environment wins over this file.** That order
makes a one-off override easy and a pipeline predictable: CI sets `LUCY_URL` and is
unaffected by whatever somebody once chose interactively on a laptop.

The file holds a credential, so it is written atomically and never printed back. On POSIX
it is created owner-only; Windows inherits the configuration directory's access controls.
`lucy config` shows it redacted.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping

CONFIG_VAR = "LUCY_CONFIG"
OWNER_ONLY = 0o600
DIRECTORY_OWNER_ONLY = 0o700

KEYS = ("url", "token", "mode")

HEADER = """\
# Written by `lucy setup`. Edit it by hand if you prefer.
#
# LUCY_URL and LUCY_TOKEN in the environment win over this file, and a --url flag wins
# over both. This file contains a credential; keep its directory private.
"""


class ConfigError(Exception):
    """The file exists and cannot be used, which is worth saying rather than ignoring."""


class Config(NamedTuple):
    """What the file said, where it lives, and what in it was not understood."""

    path: Path
    values: dict[str, str]
    unknown: tuple[str, ...]
    exists: bool

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def __repr__(self) -> str:
        return (
            f"Config(path={self.path!r}, values=<redacted>, "
            f"unknown={self.unknown!r}, exists={self.exists!r})"
        )


def config_path(environ: Mapping[str, str]) -> Path:
    """`$LUCY_CONFIG`, then the platform's configuration directory.

    XDG is honoured everywhere, because somebody who has set `XDG_CONFIG_HOME` has said
    where configuration goes. Windows falls back to `%APPDATA%`, the directory that roams
    with the profile. A machine with no home directory at all -- some service accounts --
    would make `Path.home()` raise. Refuse that state rather than reading configuration
    or saving a credential in an arbitrary working directory.
    """
    override = environ.get(CONFIG_VAR, "").strip()
    if override:
        return Path(override)
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "lucy" / "config.toml"
    appdata = environ.get("APPDATA", "").strip()
    if appdata and os.name == "nt":
        return Path(appdata) / "lucy" / "config.toml"
    try:
        home = Path.home()
    except RuntimeError as exc:
        message = "cannot find your home directory; set LUCY_CONFIG to a private configuration path"
        raise ConfigError(message) from exc
    return home / ".config" / "lucy" / "config.toml"


def load_config(environ: Mapping[str, str]) -> Config:
    """Read the file, or an empty configuration when there is not one yet.

    A missing file is the normal state before setup and is not an error. A *malformed* file
    is an error, because falling back to defaults would point the command at a different
    hub and give no clue why. A key that is not understood is neither: it is reported, so a
    typed `uri =` is visible instead of silently doing nothing.
    """
    path = config_path(environ)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Config(path=path, values={}, unknown=(), exists=False)
    except OSError as exc:
        message = f"cannot read {path}: {exc.strerror or exc}"
        raise ConfigError(message) from exc

    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        # Parser exceptions can include the offending text, which may be a credential.
        message = f"{path} is not valid UTF-8 TOML; check its encoding and quoted values"
        raise ConfigError(message) from exc

    values: dict[str, str] = {}
    for key in KEYS:
        if key not in parsed:
            continue
        value = parsed[key]
        if not isinstance(value, str):
            message = f"{path}: {key} must be a string in quotes, not {type(value).__name__}"
            raise ConfigError(message)
        values[key] = value
    unknown = tuple(sorted(key for key in parsed if key not in KEYS))
    return Config(path=path, values=values, unknown=unknown, exists=True)


def _quote(value: str) -> str:
    """A TOML basic string. Values with control characters are refused before they reach here."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render(values: Mapping[str, str]) -> str:
    """The file's exact text, keys in a fixed order so two machines diff readably."""
    for key in KEYS:
        if any(char < " " or char == "\x7f" for char in values.get(key, "")):
            message = f"{key} must not contain control characters"
            raise ConfigError(message)
    lines = [f"{key} = {_quote(values[key])}" for key in KEYS if values.get(key)]
    return HEADER + "\n" + "\n".join(lines) + "\n"


def save_config(values: Mapping[str, str], environ: Mapping[str, str]) -> Path:
    """Write the file so a crash mid-write cannot leave a half-written credential.

    The temporary file uses mode 0600 on POSIX and inherits directory access controls on
    Windows. Replacement is atomic, so a reader never sees a truncated credential. A
    failed write takes its temporary file with it.
    """
    path = config_path(environ)
    rendered = render(values)
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=DIRECTORY_OWNER_ONLY, parents=True, exist_ok=True)
        # Exclusive creation refuses symlinks and existing files. A random suffix also
        # isolates simultaneous saves from separate threads in the same process.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
        message = f"cannot write {path}: {exc.strerror or exc}"
        raise ConfigError(message) from exc
    return path


def redact(token: str) -> str:
    """Enough to recognise which token this is, never enough to use it."""
    visible = 4
    if not token:
        return ""
    return "..." + token[-visible:] if len(token) > visible * 2 else "..."
