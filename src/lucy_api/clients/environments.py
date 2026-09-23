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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.errors import UnavailableError
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

    `notice` is empty when the whole file came back and says exactly how much of how much
    arrived when it did not. Nothing here truncates silently.
    """

    path: str
    content: str = ""
    size: int = 0
    offset: int = 0
    truncated: bool = False
    notice: str = ""


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
    """

    command: str
    exit_code: int | None = None
    output: str = ""
    output_dropped_bytes: int = 0
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
        content, size = text(payload, "content"), number(payload, "size")
        truncated = flag(payload, "truncated")
        return FileText(
            path=text(payload, "path", path),
            content=content,
            size=size,
            offset=number(payload, "offset"),
            truncated=truncated,
            notice=f"showing {len(content)} of {size} bytes" if truncated else "",
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
        """Run one command through the one-call shape: open, run, return, close."""
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
        return Ran(
            command=text(payload, "command", command),
            exit_code=_exit_code(payload),
            output=text(payload, "output"),
            output_dropped_bytes=number(payload, "output_dropped_bytes"),
            timed_out=flag(payload, "timed_out"),
            state=text(payload, "state"),
        )


def _exit_code(payload: Any) -> int | None:
    """The exit code, or `None` while the command is still running.

    `None` and zero are very different answers and a default of zero would turn "still
    going" into "finished fine", which is the wrong half of that pair to guess at.
    """
    raw = field(payload, "exit_code")
    return None if raw is None else number(payload, "exit_code")


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

    def seed(self, environment: Environment, *, files: Iterable[tuple[str, str]] = ()) -> None:
        """Put one workspace in place, with whatever files a test needs in it."""
        self.workspaces[environment.environment_id] = environment
        for path, body in files:
            self.contents[(environment.environment_id, path)] = body

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
        self.contents = {
            key: body for key, body in self.contents.items() if key[0] != environment_id
        }

    async def mkdir(self, environment_id: str, path: str) -> str:
        """Directories are implicit in the fake, but the workspace must exist."""
        if environment_id not in self.workspaces:
            raise KeyError(environment_id)
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
        """The seeded file, cut at `max_bytes` so a truncation notice can be tested."""
        whole = self.contents.get((environment_id, path), "")[offset:]
        content = whole if max_bytes is None else whole[:max_bytes]
        truncated = content != whole
        return FileText(
            path=path,
            content=content,
            size=len(whole),
            offset=offset,
            truncated=truncated,
            notice=f"showing {len(content)} of {len(whole)} bytes" if truncated else "",
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
        """Whatever the test scripted, or a command that did nothing and said nothing."""
        del cwd
        self.ran.append((environment_id, command, timeout_ms, max_output_bytes))
        return self.scripted.get(command, Ran(command=command, exit_code=0, state="idle"))


if TYPE_CHECKING:

    def _satisfies(
        real: HttpEnvironmentsClient, fake: FakeEnvironmentsClient
    ) -> tuple[EnvironmentsClient, ...]:
        """Static proof that both implementations satisfy the seam."""
        return (real, fake)


__all__ = [
    "AUDIENCE",
    "DEFAULT_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
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
