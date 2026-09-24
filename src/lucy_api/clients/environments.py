"""The workspace, with the host's filesystem left on the host.

Every environment view this service returns carries a `workspace` field holding the absolute
path of the directory on the machine running it. That is an information leak in the precise
sense: it tells whatever reads it the deployment's directory layout, the account the service
runs as, and often the shape of the container -- to a model that is being asked to run
commands. It is dropped here, once, so that no caller has to remember to drop it, and there
is a test that fails if it ever comes back.

Paths inside the workspace are kept, because they are the person's own files and the whole
point of the capability. `/home/app/envs/e-3f2a/workspace/notes.md` becomes `notes.md`.

Two more things stop here. The list of credential service names a command was run with is
not projected: a service name is the one thing the model never sees, and a command that
failed for want of a login says so in its own output. And a file read carries its own
notice, because the service truncates at a byte count and a model that cannot tell it is
reading a fragment will answer confidently about the part it did not get.

The probe is `/ready`, which reports the sandbox tier in force and whether tokens can
currently be verified. Both are operator facts: a workspace is either deployed or it is not,
and no amount of connecting an account changes that.
"""

from __future__ import annotations

import base64
import codecs
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.errors import ConflictError, RejectedError, UnavailableError
from lucy_api.clients.transport import (
    Sibling,
    field,
    flag,
    given,
    moment,
    nested,
    number,
    rows,
    segment,
    text,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from lucy_api.packs.context import Http

SERVICE = "environments"
AUDIENCE = "environments-api"

READY = "ready"
DEFAULT_TIMEOUT_MS = 60_000
"""How long the sandbox may spend on one command."""

EXEC_MARGIN_SECONDS = 15.0
"""Waited on top of whatever the command was given, for the round trip around it.

The sandbox opens a shell, runs, and closes it, and charges for all three: a `git rev-parse`
asked for with a sixty-second ceiling took 15.1 seconds of wall clock, every time. So the
margin is generous on purpose -- the alternative is a caller that gives up while the thing it
asked for is still running, which is what happened to every workspace command ever run.
"""
DEFAULT_OUTPUT_BYTES = 64 * 1024
"""How much command output comes back by default. The ceiling is generous; a window is not."""
TIMED_OUT = "timed_out"
"""The command state the sandbox reports for a command it killed at its ceiling.

This state is the only sign of a timeout the sandbox sends. Its command record keeps a
`timed_out` flag but never serialises it (Environments-api app/shells/shell.py
`CommandRecord.to_dict`), so a client that read the flag alone told the model
`"timed_out": false` next to `"state": "timed_out"`.
"""
MID_CHARACTER = "validation-error"
"""The code a read at an offset inside a UTF-8 character is refused with.

Environments-api decodes a window from the byte it was asked for and will not hand back a
character with its head cut off (`ValidationError("Read offset is inside a UTF-8
character")`, app/files.py:132-136, code `validation_error`, app/errors.py:123-127). It
arrives here folded to one spelling by `problem_code`.
"""
BASE64 = "base64"
"""The encoding a binary file's content arrives in.

Environments-api calls a file binary when it holds a NUL or is not valid UTF-8, and sends
it base64 with `is_binary` set (app/file_safety.py:82-102, app/files.py:129-130). Base64
never contains a NUL, so a hub that looked for one in the content never saw a binary file,
and handed the model a `.pyc` as line-numbered base64.
"""

ARCHIVED = "archived"
"""The state the sandbox's reaper leaves a workspace in once it has sat idle past its TTL.

Its files are wiped, but it is still listed under the same name, so a hub that picks a
workspace by name picks this one. Every file and shell call on it is then refused until it
is reset, which is the only way back.
"""
ARCHIVED_CODE = "environment-archived"
"""The problem code of that refusal, folded the way `problem_code` folds every code.

The sandbox sends `environment_archived` with a 409, and a 409 alone does not say which
state the call ran into: a full quota is a 409 as well, and resetting on that one would
wipe a workspace that was working.
"""


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a workspace can be had at all, and how isolated it would be."""

    ready: bool
    sandbox_tier: str = ""
    keyring_status: str = ""


@dataclass(frozen=True, slots=True)
class Environment:
    """One workspace, without the host path it lives at.

    `disk_bytes` and `shells_running` are here because they are what a person asks about
    when something is slow, and they cost a number each.
    """

    environment_id: str
    name: str
    profile: str = ""
    state: str = ""
    sandbox_tier: str = ""
    network: bool = False
    shells_running: int = 0
    disk_bytes: int = 0
    last_activity_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Entry:
    """One row of a directory listing, addressed the way the person addresses it."""

    name: str
    path: str
    kind: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class Listing:
    """What is in one directory of the workspace."""

    path: str
    entries: tuple[Entry, ...] = ()


@dataclass(frozen=True, slots=True)
class FileText:
    """A read of one file, which may be part of one.

    `notice` is empty when the whole file came back and names the exact bytes that arrived,
    and the offset to read on from, when it did not. Nothing here truncates silently.
    `offset`, `next_offset` and `size` are byte positions, never character counts.

    `binary` is the service's verdict on the whole file, not on the window. When it is set,
    `content` is base64 and is not the file's text to anything that reads it.
    """

    path: str
    content: str = ""
    size: int = 0
    offset: int = 0
    next_offset: int = 0
    truncated: bool = False
    notice: str = ""
    binary: bool = False


@dataclass(frozen=True, slots=True)
class Written:
    """What a write left behind."""

    path: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class Mutation:
    """A mutation with its reviewable diff and optimistic validator."""

    path: str
    size: int = 0
    etag: str = ""
    diff: str = ""
    applied_hunks: tuple[int, ...] = ()
    rejected_hunks: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    lines: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[SearchMatch, ...] = ()
    total_matches: int = 0
    truncated: bool = False
    skipped_binary: tuple[str, ...] = ()
    skipped_large: tuple[str, ...] = ()
    skipped_unavailable: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Ran:
    """One command and what it produced.

    `output_dropped_bytes` is the service's own count of what fell out of the ring buffer
    between the start of the command and the read, and it is carried for the same reason as
    every other notice: a gap nobody mentions is a gap nobody can account for.

    `output_truncated_bytes` is the other gap, at the far end: `output` is the *head* of what
    the command printed, cut at `max_output_bytes`, and this is how much came after it. The
    ring buffer count never included it, so a megabyte build log was reported as a few
    thousand characters omitted, with the failure summary at its end never mentioned.
    """

    command: str
    exit_code: int | None = None
    output: str = ""
    output_dropped_bytes: int = 0
    output_truncated_bytes: int = 0
    timed_out: bool = False
    state: str = ""


class EnvironmentsClient(Protocol):
    """The workspace operations Lucy needs: one to get one, three to use it, one to run."""

    async def ready(self) -> Readiness:
        """Whether this deployment can give somebody a workspace."""
        ...

    async def create(self, name: str, *, profile: str = "") -> Environment:
        """Make a workspace under this person's account and profile."""
        ...

    async def environments(self, *, profile: str = "") -> tuple[Environment, ...]:
        """Every workspace this person has."""
        ...

    async def destroy(self, environment_id: str) -> None:
        """Permanently remove one workspace and everything inside it."""
        ...

    async def reset(self, environment_id: str) -> Environment:
        """Wipe one workspace and make it usable again, which brings an archived one back."""
        ...

    async def mkdir(self, environment_id: str, path: str) -> str:
        """Create a directory and any missing parents."""
        ...

    async def files(self, environment_id: str, path: str = ".") -> Listing:
        """List one directory."""
        ...

    async def read(
        self, environment_id: str, path: str, *, offset: int = 0, max_bytes: int | None = None
    ) -> FileText:
        """Read one file, or part of one."""
        ...

    async def write(
        self, environment_id: str, path: str, content: str, *, mode: str = "overwrite"
    ) -> Written:
        """Write one file, creating the directories above it."""
        ...

    async def search(self, environment_id: str, path: str, pattern: str) -> SearchResult: ...

    async def edit(
        self, environment_id: str, path: str, old_string: str, new_string: str
    ) -> Mutation: ...

    async def patch(self, environment_id: str, path: str, patch: str) -> Mutation: ...

    async def delete(self, environment_id: str, path: str, *, recursive: bool = False) -> None: ...

    async def move(self, environment_id: str, source: str, destination: str) -> Mutation: ...

    async def run(
        self,
        environment_id: str,
        command: str,
        *,
        cwd: str = ".",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> Ran:
        """Run one command in a fresh shell and close it afterwards, whatever happened."""
        ...


class HttpEnvironmentsClient:
    """The real client, over the one seam a capability has to a sibling."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def ready(self) -> Readiness:
        """Read `/ready`, reporting an outage as "not ready" rather than raising."""
        try:
            payload = await self._api.send("GET", "/ready")
        except UnavailableError as outage:
            payload = {"status": "not_ready", "keyring": {"status": outage.detail or "unreachable"}}
        return Readiness(
            ready=text(payload, "status") == READY,
            sandbox_tier=text(payload, "sandbox_tier"),
            keyring_status=text(nested(payload, "keyring"), "status"),
        )

    async def create(self, name: str, *, profile: str = "") -> Environment:
        """Create a workspace. The profile decides which credential set it may reach."""
        payload = await self._api.send(
            "POST", "/v1/environments", body={"name": name}, profile=profile
        )
        return _environment(payload)

    async def environments(self, *, profile: str = "") -> tuple[Environment, ...]:
        """Every workspace on this profile."""
        payload = await self._api.send("GET", "/v1/environments", profile=profile)
        return tuple(_environment(row) for row in rows(payload, "environments"))

    async def destroy(self, environment_id: str) -> None:
        """Delete a workspace after a failed provisioning attempt or session cleanup."""
        await self._api.send("DELETE", f"/v1/environments/{segment(environment_id)}")

    async def reset(self, environment_id: str) -> Environment:
        """Wipe a workspace and leave it active. The service answers with its new view."""
        payload = await self._api.send("POST", f"/v1/environments/{segment(environment_id)}/reset")
        return _environment(payload)

    async def mkdir(self, environment_id: str, path: str) -> str:
        """Create a directory tree and return its normalized relative path."""
        payload = await self._api.send(
            "POST",
            f"/v1/environments/{segment(environment_id)}/files/directories",
            body={"path": path},
        )
        return text(payload, "path", path)

    async def files(self, environment_id: str, path: str = ".") -> Listing:
        """One directory, with each entry addressed relative to the workspace root."""
        payload = await self._api.send(
            "GET", f"/v1/environments/{segment(environment_id)}/files", params={"path": path}
        )
        return Listing(
            path=text(payload, "path", path),
            entries=tuple(_entry(row) for row in rows(payload, "entries")),
        )

    async def read(
        self, environment_id: str, path: str, *, offset: int = 0, max_bytes: int | None = None
    ) -> FileText:
        """Read a file, confessing in `notice` when only part of it came back."""
        payload = await self._api.send(
            "GET",
            f"/v1/environments/{segment(environment_id)}/files/content",
            params=given(path=path, offset=offset, max_bytes=max_bytes),
        )
        start, size = number(payload, "offset"), number(payload, "size")
        end, truncated = number(payload, "next_offset", start), flag(payload, "truncated")
        return FileText(
            path=text(payload, "path", path),
            content=text(payload, "content"),
            size=size,
            offset=start,
            next_offset=end,
            truncated=truncated,
            notice=_window_notice(start, end, size, truncated=truncated),
            binary=flag(payload, "is_binary") or text(payload, "encoding") == BASE64,
        )

    async def write(
        self, environment_id: str, path: str, content: str, *, mode: str = "overwrite"
    ) -> Written:
        """Write a file. `mode` is `overwrite` or `append`, as the service names them."""
        payload = await self._api.send(
            "PUT",
            f"/v1/environments/{segment(environment_id)}/files/content",
            body={"path": path, "content": content, "encoding": "utf-8", "mode": mode},
        )
        return Written(path=text(payload, "path", path), size=number(payload, "size"))

    async def search(self, environment_id: str, path: str, pattern: str) -> SearchResult:
        payload = await self._api.send(
            "GET",
            f"/v1/environments/{segment(environment_id)}/files/search",
            params={"path": path, "pattern": pattern, "mode": "content"},
        )
        return SearchResult(
            matches=tuple(
                SearchMatch(path=text(row, "path"), lines=tuple(rows(row, "lines")))
                for row in rows(payload, "matches")
            ),
            total_matches=number(payload, "total_matches"),
            truncated=flag(payload, "truncated"),
            skipped_binary=tuple(str(item) for item in rows(payload, "skipped_binary")),
            skipped_large=tuple(str(item) for item in rows(payload, "skipped_large")),
            skipped_unavailable=tuple(str(item) for item in rows(payload, "skipped_unavailable")),
        )

    async def edit(
        self, environment_id: str, path: str, old_string: str, new_string: str
    ) -> Mutation:
        payload = await self._api.send(
            "POST",
            f"/v1/environments/{segment(environment_id)}/files/edit",
            body={"path": path, "old_string": old_string, "new_string": new_string},
        )
        return _mutation(payload)

    async def patch(self, environment_id: str, path: str, patch: str) -> Mutation:
        payload = await self._api.send(
            "POST",
            f"/v1/environments/{segment(environment_id)}/files/patch",
            body={"path": path, "patch": patch},
        )
        return _mutation(payload)

    async def delete(self, environment_id: str, path: str, *, recursive: bool = False) -> None:
        await self._api.send(
            "DELETE",
            f"/v1/environments/{segment(environment_id)}/files/content",
            params={"path": path, "recursive": recursive},
        )

    async def move(self, environment_id: str, source: str, destination: str) -> Mutation:
        payload = await self._api.send(
            "POST",
            f"/v1/environments/{segment(environment_id)}/files/move",
            body={"source": source, "destination": destination},
        )
        return _mutation(payload)

    async def run(
        self,
        environment_id: str,
        command: str,
        *,
        cwd: str = ".",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> Ran:
        """Run one command through the one-call shape: open, run, return, close.

        The command that comes back is the one sent, never the sandbox's echo of it. The
        sandbox runs `( command\\n)` in a subshell and echoes that string (Environments-api
        app/api/routes/exec.py), so the model was shown `"( ls -la\\n)"` for every `ls -la`
        it ran. The hub knows what it asked for, and the echo adds nothing to that.
        """
        body = {
            "environment_id": environment_id,
            "command": command,
            "cwd": cwd,
            "timeout_ms": timeout_ms,
            "max_output_bytes": max_output_bytes,
        }
        # Longer than the command's own ceiling, necessarily: waiting less than the work you
        # asked for is a failure you have arranged yourself.
        payload = await self._api.send(
            "POST",
            "/v1/exec",
            body=body,
            timeout_seconds=timeout_ms / 1000 + EXEC_MARGIN_SECONDS,
        )
        state = text(payload, "state")
        return Ran(
            command=command,
            exit_code=_exit_code(payload),
            output=text(payload, "output"),
            output_dropped_bytes=number(payload, "output_dropped_bytes"),
            output_truncated_bytes=_truncated_bytes(payload),
            timed_out=flag(payload, "timed_out") or state == TIMED_OUT,
            state=state,
        )


def _truncated_bytes(payload: Any) -> int:
    """How much output the command printed after the part that came back.

    The sandbox reads from the start of the command and stops at `max_output_bytes`
    (Environments-api app/api/routes/shells.py `command_result`), and says so only in two
    byte offsets: `output_cursor`, where the read stopped, and `output_end`, where the
    command's output did. A command still running has no end yet, and then nothing here can
    say how much there is, so it counts as nothing rather than as a guess.
    """
    end, cursor = field(payload, "output_end"), field(payload, "output_cursor")
    if end is None or cursor is None:
        return 0
    return max(0, number(payload, "output_end") - number(payload, "output_cursor"))


def _exit_code(payload: Any) -> int | None:
    """The exit code, or `None` while the command is still running.

    `None` and zero are very different answers and a default of zero would turn "still
    going" into "finished fine", which is the wrong half of that pair to guess at.
    """
    raw = field(payload, "exit_code")
    return None if raw is None else number(payload, "exit_code")


def _window_notice(start: int, end: int, size: int, *, truncated: bool) -> str:
    """Which bytes of how many a read returned, and where the next window starts.

    Byte positions only, because the model's next call takes one. The notice this replaced
    said `showing 5 of 13 bytes` for a six-byte window of "héllo wörld": it counted decoded
    characters, overcounted base64 by a third, and never said where the window began, so a
    model reading on could only guess an offset.
    """
    if not truncated and not start:
        return ""
    shown = f"showing bytes {start}-{end} of {size}"
    return f"{shown}; continue with offset={end}" if truncated else shown


def _environment(payload: Any) -> Environment:
    """One environment, without the host workspace path. That omission is the point."""
    return Environment(
        environment_id=text(payload, "id"),
        name=text(payload, "name"),
        profile=text(payload, "profile"),
        state=text(payload, "state"),
        sandbox_tier=text(payload, "sandbox_tier"),
        network=flag(payload, "network"),
        shells_running=number(payload, "shells_running"),
        disk_bytes=number(payload, "disk_bytes"),
        last_activity_at=moment(field(payload, "last_activity_at")),
    )


def _entry(row: Any) -> Entry:
    """One directory row, without its modification time, which nothing here reads."""
    return Entry(
        name=text(row, "name"),
        path=text(row, "path"),
        kind=text(row, "kind"),
        size=number(row, "size"),
    )


def _mutation(payload: Any) -> Mutation:
    return Mutation(
        path=text(payload, "path"),
        size=number(payload, "size"),
        etag=text(payload, "etag"),
        diff=text(payload, "diff"),
        applied_hunks=tuple(int(item) for item in rows(payload, "applied_hunks")),
        rejected_hunks=tuple(int(item) for item in rows(payload, "rejected_hunks")),
    )


class FakeEnvironmentsClient:
    """An in-memory workspace, so a workspace pack's tests need no sandbox and no disk.

    Files are a dictionary keyed by workspace and path, and commands are scripted: this fake
    is not a shell, and a test that needs a real one is a test of environments-api rather
    than of the hub. What it does model faithfully is that two workspaces are two different
    filesystems, because a client that addressed the wrong one would still pass otherwise.
    """

    def __init__(self) -> None:
        self.readiness = Readiness(ready=True, sandbox_tier="container", keyring_status="ok")
        self.workspaces: dict[str, Environment] = {}
        self.contents: dict[tuple[str, str], str] = {}
        self.scripted: dict[str, Ran] = {}
        self.ran: list[tuple[str, str, int, int]] = []
        self.wrote: list[tuple[str, str]] = []
        self.resets: list[str] = []

    def seed(self, environment: Environment, *, files: Iterable[tuple[str, str]] = ()) -> None:
        """Put one workspace in place, with whatever files a test needs in it."""
        self.workspaces[environment.environment_id] = environment
        for path, body in files:
            self.contents[(environment.environment_id, path)] = body

    def archive(self, environment_id: str) -> None:
        """Do what the sandbox's reaper does to an idle workspace: wipe it and keep listing it."""
        self.workspaces[environment_id] = replace(self.workspaces[environment_id], state=ARCHIVED)
        self._wipe(environment_id)

    def _wipe(self, environment_id: str) -> None:
        self.contents = {
            key: body for key, body in self.contents.items() if key[0] != environment_id
        }

    def script(self, command: str, result: Ran) -> None:
        """Say what one command will do, because this fake cannot actually run it."""
        self.scripted[command] = result

    async def ready(self) -> Readiness:
        """Whatever the test said about this deployment."""
        return self.readiness

    async def create(self, name: str, *, profile: str = "") -> Environment:
        """Register a workspace named as asked, on the profile that asked for it."""
        environment = Environment(
            environment_id=f"env-{len(self.workspaces) + 1}", name=name, profile=profile
        )
        self.workspaces[environment.environment_id] = environment
        return environment

    async def environments(self, *, profile: str = "") -> tuple[Environment, ...]:
        """Every seeded workspace, narrowed to one profile when a profile was named."""
        return tuple(
            item for item in self.workspaces.values() if not profile or item.profile == profile
        )

    async def destroy(self, environment_id: str) -> None:
        """Remove a fake workspace and all of its files."""
        self.workspaces.pop(environment_id, None)
        self._wipe(environment_id)

    async def reset(self, environment_id: str) -> Environment:
        """Wipe the workspace and make it active, whatever state it was in, as the service does."""
        self.resets.append(environment_id)
        environment = replace(self.workspaces[environment_id], state="active")
        self.workspaces[environment_id] = environment
        self._wipe(environment_id)
        return environment

    async def mkdir(self, environment_id: str, path: str) -> str:
        """Directories are implicit in the fake, but the workspace must exist and be usable.

        An archived one is refused here with the sandbox's own 409 and code. The sandbox
        refuses every file and shell call the same way; this is the one the hub makes first
        when it provisions or resets a session, so it is where the refusal is met.
        """
        environment = self.workspaces.get(environment_id)
        if environment is None:
            raise KeyError(environment_id)
        if environment.state == ARCHIVED:
            detail = f"environment {environment_id} is archived; reset it first"
            raise ConflictError(SERVICE, 409, detail, ARCHIVED_CODE)
        return path

    async def files(self, environment_id: str, path: str = ".") -> Listing:
        """The seeded files of one workspace that sit under `path`, as one flat listing."""
        prefix = "" if path in {".", ""} else f"{path}/"
        return Listing(
            path=path,
            entries=tuple(
                Entry(name=name.removeprefix(prefix), path=name, kind="file", size=len(body))
                for (workspace, name), body in sorted(self.contents.items())
                if workspace == environment_id and name.startswith(prefix)
            ),
        )

    async def read(
        self, environment_id: str, path: str, *, offset: int = 0, max_bytes: int | None = None
    ) -> FileText:
        """The seeded file as environments-api serves it: a window of its UTF-8 bytes.

        Offsets and sizes are byte counts, the window ends on a character boundary, and one
        that starts inside a character is refused with the service's own 422 (app/files.py:
        127-138). A fake that sliced the str could never refuse, so a tail read at a
        computed offset passed here and failed against the sandbox whenever it landed
        inside a `✓`. A body with a NUL in it is binary, as it is to the service, and its
        window comes back base64.
        """
        data = self.contents.get((environment_id, path), "").encode()
        window = data[offset:] if max_bytes is None else data[offset : offset + max_bytes]
        binary = b"\0" in data
        if binary:
            content = base64.b64encode(window).decode("ascii")
        else:
            decoder = codecs.getincrementaldecoder("utf-8")()
            try:
                content = decoder.decode(window)
            except UnicodeDecodeError as exc:
                detail = "Read offset is inside a UTF-8 character"
                raise RejectedError(SERVICE, 422, detail, MID_CHARACTER) from exc
            window = window[: len(window) - len(decoder.getstate()[0])]
        end = offset + len(window)
        truncated = end < len(data)
        return FileText(
            path=path,
            content=content,
            size=len(data),
            offset=offset,
            next_offset=end,
            truncated=truncated,
            notice=_window_notice(offset, end, len(data), truncated=truncated),
            binary=binary,
        )

    async def write(
        self, environment_id: str, path: str, content: str, *, mode: str = "overwrite"
    ) -> Written:
        """Store the file in that workspace, appending when asked to append."""
        self.wrote.append((environment_id, path))
        key = (environment_id, path)
        existing = self.contents.get(key, "") if mode == "append" else ""
        self.contents[key] = existing + content
        return Written(path=path, size=len(self.contents[key]))

    async def search(self, environment_id: str, path: str, pattern: str) -> SearchResult:
        prefix = "" if path in {"", "."} else f"{path}/"
        matches = tuple(
            SearchMatch(
                name,
                tuple(
                    {"line": number_, "text": line, "matched": True}
                    for number_, line in enumerate(body.splitlines(), 1)
                    if pattern in line
                ),
            )
            for (workspace, name), body in sorted(self.contents.items())
            if workspace == environment_id and name.startswith(prefix) and pattern in body
        )
        return SearchResult(matches=matches, total_matches=len(matches))

    async def edit(
        self, environment_id: str, path: str, old_string: str, new_string: str
    ) -> Mutation:
        key = (environment_id, path)
        old = self.contents.get(key, "")
        self.contents[key] = old.replace(old_string, new_string, 1)
        return Mutation(path, len(self.contents[key]), diff=f"-{old}\n+{self.contents[key]}")

    async def patch(self, environment_id: str, path: str, patch: str) -> Mutation:
        self.contents[(environment_id, path)] = patch
        return Mutation(path, len(patch), applied_hunks=(1,))

    async def delete(self, environment_id: str, path: str, *, recursive: bool = False) -> None:
        if not recursive:
            self.contents.pop((environment_id, path), None)
            return
        prefix = path.rstrip("/")
        self.contents = {
            key: body
            for key, body in self.contents.items()
            if key[0] != environment_id
            or (key[1] != prefix and not key[1].startswith(f"{prefix}/"))
        }

    async def move(self, environment_id: str, source: str, destination: str) -> Mutation:
        body = self.contents.pop((environment_id, source))
        self.contents[(environment_id, destination)] = body
        return Mutation(destination, len(body))

    async def run(
        self,
        environment_id: str,
        command: str,
        *,
        cwd: str = ".",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> Ran:
        """Whatever the test scripted, or a command that did nothing and said nothing.

        A scripted `timed_out` state reads as a timeout whether or not the script also set
        the flag, because that is what the real client makes of the same answer. Scripted
        output longer than `max_output_bytes` comes back as its head and a count of the
        rest, because that is what the sandbox does with it. And the command is the one
        asked for, whatever the script called it, as the real client reports it.
        """
        del cwd
        self.ran.append((environment_id, command, timeout_ms, max_output_bytes))
        result = self.scripted.get(command, Ran(command=command, exit_code=0, state="idle"))
        printed = result.output.encode()
        if len(printed) > max_output_bytes:
            cut = len(printed) - max_output_bytes
            result = replace(
                result,
                output=printed[:max_output_bytes].decode("utf-8", "replace"),
                output_truncated_bytes=result.output_truncated_bytes + cut,
            )
        return replace(
            result, command=command, timed_out=result.timed_out or result.state == TIMED_OUT
        )


if TYPE_CHECKING:

    def _satisfies(
        real: HttpEnvironmentsClient, fake: FakeEnvironmentsClient
    ) -> tuple[EnvironmentsClient, ...]:
        """Static proof that both implementations satisfy the seam."""
        return (real, fake)


__all__ = [
    "ARCHIVED",
    "ARCHIVED_CODE",
    "AUDIENCE",
    "BASE64",
    "DEFAULT_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "MID_CHARACTER",
    "SERVICE",
    "Entry",
    "Environment",
    "EnvironmentsClient",
    "FakeEnvironmentsClient",
    "FileText",
    "HttpEnvironmentsClient",
    "Listing",
    "Mutation",
    "Ran",
    "Readiness",
    "SearchMatch",
    "SearchResult",
    "Written",
]
