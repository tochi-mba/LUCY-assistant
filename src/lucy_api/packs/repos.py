"""Repositories, exposed as the product word ``repos``: code, pull requests, issues and CI.

The model never learns that GitHub, a port or a route is behind this. It sees `repos.pulls`,
`repos.merge`, `repos.watch`, each named for what a person asks for. The provider shows up in
one place only -- the capability's live line, "GitHub as @octo", because that is the account
the person connected and knowing which one is theirs to know.

**What runs without asking is the person's choice, permission by permission.** Every write is
declared under one of seven permissions sized the way a person thinks about them: commenting
is not merging, and merging is not deleting a repository. Each is asked about with the
ordinary approval card -- once, this conversation, this profile, or always, and optionally
"always, for this repository" -- or decided in advance through `/v1/permissions`. Anything
other people will see is `outward`, so even `auto` asks until the person has said yes; deleting
or changing visibility is `destructive`, so `auto` always asks under the default policy. How
much GitHub itself lets the connection do is a separate, earlier choice, made at GitHub when
the person picked repositories for the app or the scopes of their token.

**Every write names its repository in plain text** (`repo: "owner/name"`, `number: 42`), never
by `$reference`. The approval gate reads a call's arguments before anything runs, and "always,
for this repository" can only match a repository it can see.

**`repos.watch` is a subscription** (`docs/jobs.md`): Github-api watches, and signals Lucy when
CI settles, a pull request merges or a review lands. With `wake`, the session is woken and the
turn acts under the standing consent the person gave when they asked -- which is what makes
"merge it when CI is green" one request rather than two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import (
    array_schema,
    boolean_schema,
    enum_schema,
    integer_schema,
    number_schema,
    object_schema,
    record_schema,
    string_schema,
)
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import (
    AbsentError,
    ConflictError,
    DownstreamError,
    ForbiddenError,
    NotConnectedError,
    PreconditionError,
    RateLimitedError,
    RejectedError,
)
from lucy_api.clients.repos import AUDIENCE, MAX_LIMIT, HttpReposClient
from lucy_api.context.types import Trust
from lucy_api.packs import repos_calls as calls
from lucy_api.packs.base import Availability, Permission, SetupPlan, SetupStep, State
from lucy_api.packs.collections import CHECK, ISSUE, PULL, REPO
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.packs.repos_calls import (
    DEFAULT_WATCH_SECONDS,
    ReposInputError,
    full_name,
    repo_field,
    watch_seconds,
    watch_target_fits,
)
from lucy_api.prompt.docs import capability_doc
from lucy_api.work.subscriptions import Signal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.repos import ReposClient
    from lucy_api.packs.context import Http, PackContext


WATCH_KINDS = ("checks_settled", "pull_merged", "review_submitted", "run_completed")

NOT_CONNECTED = (
    "Repositories are not connected for this profile, or the connection lapsed. Offer the "
    "person the link from capabilities.setup for `repos`. Never ask them for a token."
)
WATCH_NEEDS = {
    "checks_settled": "`number` (a pull request) or `ref` (a branch or commit)",
    "pull_merged": "`number`",
    "review_submitted": "`number`",
    "run_completed": "`run`",
}
NO_SUBSCRIPTIONS = "Watching repositories is not available in this conversation."


class ReposRefusedError(ValueError):
    """The service refused a call, for a reason the model should read and act on."""


class ReposPack:
    """Find, read, change and watch repositories, without exposing the provider."""

    id = "repos"
    title = "Repositories"
    summary = (
        "Read and change code, pull requests and issues, run and watch CI, and create or "
        "remove repositories, on the account the person connected."
    )

    def __init__(
        self, base_url: str, *, audience: str = AUDIENCE, client: ReposClient | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._override = client

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="repos.comment",
                title="Comment and open issues on your repositories",
                description="Post comments and reviews, and open or close issues.",
                risk="write",
                covers=("repos.comment", "repos.review", "repos.openIssue", "repos.closeIssue"),
                outward=True,
                tally="repo",
            ),
            Permission(
                id="repos.change",
                title="Push changes and open pull requests",
                description="Commit files, create branches, and open or update pull requests.",
                risk="write",
                covers=("repos.commit", "repos.branch", "repos.openPull", "repos.updatePull"),
                outward=True,
                tally="repo",
            ),
            Permission(
                id="repos.merge",
                title="Merge pull requests",
                description="Merge a pull request into its base branch.",
                risk="write",
                covers=("repos.merge",),
                outward=True,
                tally="repo",
            ),
            Permission(
                id="repos.ci",
                title="Run, re-run or cancel CI",
                description="Start a workflow, re-run failed jobs, or cancel a run.",
                risk="execute",
                covers=("repos.rerun", "repos.dispatch", "repos.cancelRun"),
                tally="repo",
            ),
            Permission(
                id="repos.create",
                title="Create repositories",
                description="Create a new repository, private unless the person says otherwise.",
                risk="write",
                covers=("repos.create",),
                outward=True,
                tally="name",
            ),
            Permission(
                id="repos.destroy",
                title="Delete repositories and branches, or change who can see a repository",
                description="Delete a repository or a branch, or make a repository public or "
                "private. A deleted repository cannot be brought back from here.",
                risk="destructive",
                covers=("repos.delete", "repos.deleteBranch", "repos.setVisibility"),
                outward=True,
                tally="repo",
            ),
            Permission(
                id="repos.watch",
                title="Keep watching a repository and act when it changes",
                description="Ask to be told when CI settles, a pull request merges or a review "
                "lands; when it wakes the conversation, finish what was asked under your "
                "standing consent, which you can revoke at any time.",
                risk="write",
                covers=("repos.watch",),
                tally="repo",
            ),
        )

    def result_trust(self, operation: str, data: object) -> Trust:
        """Titles, bodies, comments and logs are written by other people and their tools."""
        del data
        return Trust.observed if operation == "repos.me" else Trust.untrusted

    def setup(self) -> SetupPlan | None:
        return SetupPlan(
            summary=(
                "Connect a repository account in the browser, choosing which repositories Lucy "
                "may reach; never paste a password or token into a conversation."
            ),
            steps=(
                SetupStep(
                    id="connect",
                    kind="oauth",
                    title="Install and approve the app",
                    description="Open Lucy's connection link, choose all repositories or a "
                    "few, and approve. What GitHub grants here is the most Lucy can ever do.",
                ),
                SetupStep(
                    id="token",
                    kind="api_key",
                    title="Or store a fine-grained token in the vault",
                    description="Create a fine-grained token with exactly the repositories and "
                    "permissions you want, and store it in the identity vault for this "
                    "profile. Never paste it into a conversation.",
                    required=False,
                ),
            ),
        )

    async def probe(self, context: PackContext) -> Availability:
        try:
            identity = await self._client(context).me(context.profile)
        except NotConnectedError:
            return Availability(
                state=State.not_connected,
                detail="connect a repository account to read and change repositories",
            )
        except (NoBrokerError, ExchangeError):
            return Availability(state=State.unavailable, detail="cannot act for this person yet")
        except (DownstreamError, TransportError):
            return Availability(state=State.unavailable, detail="repositories could not be reached")
        reach = (
            "all repositories"
            if identity.selection == "all"
            else (f"{identity.repositories} chosen repositories")
        )
        how = "with a token" if identity.kind == "pat" else "through the app"
        return Availability(
            state=State.ready, detail=f"GitHub as @{identity.login}, {reach}, {how}"
        )

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        return (*self._reads(), *self._writes(), self._watch(context.policy.wake_by_default))

    # ------------------------------------------------------------------ definitions

    def _op(  # noqa: PLR0913 - an operation is these six facts; the call sites read as a table
        self,
        name: str,
        description: str,
        fields: dict[str, Any],
        handler: Callable[[RunContext[PackContext], ReposClient], Awaitable[Any]],
        *,
        output: Any = None,
        effects: str = "read",
    ) -> AnyOperation:
        async def run(run: RunContext[PackContext]) -> Any:
            try:
                return await handler(run, self._client(run.ctx))
            except NotConnectedError as exc:
                raise ReposRefusedError(NOT_CONNECTED) from exc
            except RateLimitedError as exc:
                wait = f" Try again in {exc.retry_after:.0f}s." if exc.retry_after else ""
                message = f"The repository host is limiting requests.{wait}"
                raise ReposRefusedError(message) from exc
            except (
                AbsentError,
                ConflictError,
                ForbiddenError,
                PreconditionError,
                RejectedError,
            ) as exc:
                raise ReposRefusedError(exc.detail or str(exc)) from exc

        return define_operation(
            {
                "name": name,
                "description": description,
                "input": object_schema(fields),
                "output": output if output is not None else value(object_schema({})),
                "effects": effects,  # type: ignore[typeddict-item]
                "run": run,
            }
        )

    def _reads(self) -> tuple[AnyOperation, ...]:
        repo = repo_field("The repository, as owner/name.")
        number = integer_schema().min(1).describe("The pull request or issue number.")
        limit = integer_schema().optional().describe(f"At most this many, up to {MAX_LIMIT}.")
        state = enum_schema("open", "closed", "all").optional().describe("Default open.")
        return (
            self._op(
                "repos.me",
                "Which account is connected and how many repositories it can reach.",
                {},
                calls.me,
            ),
            self._op(
                "repos.find",
                "Find repositories the account can reach, by part of the name or by owner.",
                {
                    "query": string_schema().optional(),
                    "owner": string_schema().optional(),
                    "limit": limit,
                },
                calls.find,
                output=REPO,
            ),
            self._op(
                "repos.inspect",
                "One repository: visibility, default branch, and CI on the default branch.",
                {"repo": repo},
                calls.inspect,
            ),
            self._op(
                "repos.pulls",
                "List pull requests on a repository, newest first.",
                {"repo": repo, "state": state, "limit": limit},
                calls.pulls,
                output=PULL,
            ),
            self._op(
                "repos.pull",
                "Read one pull request: its description, reviews, open review threads, checks "
                "and whether it can be merged.",
                {"repo": repo, "number": number},
                calls.read_pull,
            ),
            self._op(
                "repos.changes",
                "What a pull request changes: each file with its status, lines added and "
                "removed, and its patch. A large patch is cut, and says so.",
                {"repo": repo, "number": number, "limit": limit},
                calls.changes,
            ),
            self._op(
                "repos.issues",
                "List issues on a repository, newest first.",
                {"repo": repo, "state": state, "limit": limit},
                calls.issues,
                output=ISSUE,
            ),
            self._op(
                "repos.checks",
                "CI on a pull request (`number`) or a branch or commit (`ref`): each job and "
                "how it ended, with the failing steps of a failed one.",
                {
                    "repo": repo,
                    "number": integer_schema().min(1).optional(),
                    "ref": string_schema().optional(),
                },
                calls.checks,
                output=CHECK,
            ),
            self._op(
                "repos.log",
                f"Part of one CI job's log: about {calls.LOG_LINES} lines, from the first line "
                "containing `starting_at` when given, else the end, where the failure usually is.",
                {
                    "repo": repo,
                    "run": string_schema().describe("The job's id, as repos.checks gave it."),
                    "starting_at": string_schema().optional(),
                },
                calls.log,
            ),
            self._op(
                "repos.read",
                "Read one file at a branch, tag or commit (default branch when `ref` is omitted).",
                {"repo": repo, "path": string_schema(), "ref": string_schema().optional()},
                calls.read,
            ),
            self._op(
                "repos.tree",
                "List the files under a directory at a ref.",
                {
                    "repo": repo,
                    "path": string_schema().optional(),
                    "ref": string_schema().optional(),
                },
                calls.tree,
            ),
        )

    def _writes(self) -> tuple[AnyOperation, ...]:
        repo = repo_field("The repository, as owner/name, written out -- never a reference.")
        number = integer_schema().min(1).describe("The pull request or issue number.")
        body = string_schema().describe("Markdown, written for the people who will read it.")
        branch = string_schema().describe("A branch name.")
        return (
            self._op(
                "repos.create",
                "Create a repository, private unless `visibility` says otherwise.",
                {
                    "name": string_schema().regex(r"^[A-Za-z0-9._-]{1,100}$"),
                    "owner": string_schema()
                    .optional()
                    .describe(
                        "An organisation to create it in; the person's account when omitted."
                    ),
                    "visibility": enum_schema("private", "public", "internal").optional(),
                    "description": string_schema().optional(),
                },
                calls.create,
                effects="write",
            ),
            self._op(
                "repos.setVisibility",
                "Make a repository public or private.",
                {"repo": repo, "visibility": enum_schema("private", "public", "internal")},
                calls.set_visibility,
                effects="write",
            ),
            self._op(
                "repos.delete",
                "Delete a repository. It cannot be brought back from here.",
                {"repo": repo},
                calls.delete,
                effects="write",
            ),
            self._op(
                "repos.branch",
                "Create a branch from another branch, tag or commit (the default branch when "
                "`start` is omitted).",
                {"repo": repo, "name": branch, "start": string_schema().optional()},
                calls.branch,
                effects="write",
            ),
            self._op(
                "repos.deleteBranch",
                "Delete a branch.",
                {"repo": repo, "name": branch},
                calls.delete_branch,
                effects="write",
            ),
            self._op(
                "repos.commit",
                f"Write up to {calls.MAX_FILES_PER_COMMIT} whole files to a branch in one commit, "
                "creating the branch from `base` when it does not exist yet.",
                {
                    "repo": repo,
                    "branch": branch,
                    "message": string_schema().describe("The commit message."),
                    "files": array_schema(
                        object_schema({"path": string_schema(), "content": string_schema()})
                    ).min(1),
                    "base": string_schema().optional(),
                },
                calls.commit,
                effects="write",
            ),
            self._op(
                "repos.openPull",
                "Open a pull request from `head` into `base` (the default branch when omitted).",
                {
                    "repo": repo,
                    "title": string_schema(),
                    "head": branch,
                    "base": string_schema().optional(),
                    "body": body.optional(),
                    "draft": boolean_schema()
                    .optional()
                    .describe("Omit unless the person said: their settings decide, else ready."),
                },
                calls.open_pull,
                effects="write",
            ),
            self._op(
                "repos.updatePull",
                "Change a pull request's title, description or draft state, or close it.",
                {
                    "repo": repo,
                    "number": number,
                    "title": string_schema().optional(),
                    "body": body.optional(),
                    "draft": boolean_schema().optional(),
                    "state": enum_schema("open", "closed").optional(),
                },
                calls.update_pull,
                effects="write",
            ),
            self._op(
                "repos.merge",
                "Merge a pull request. Read it with repos.pull first: a blocked or conflicted "
                "one is refused with the reason.",
                {
                    "repo": repo,
                    "number": number,
                    "method": enum_schema("merge", "squash", "rebase")
                    .optional()
                    .describe("Omit unless the person said: their settings decide, else squash."),
                    "delete_branch": boolean_schema()
                    .optional()
                    .describe("Omit unless the person said: their settings decide, else kept."),
                },
                calls.merge,
                effects="write",
            ),
            self._op(
                "repos.review",
                "Approve, request changes on, or comment on a pull request as a review.",
                {
                    "repo": repo,
                    "number": number,
                    "event": enum_schema("approve", "request_changes", "comment"),
                    "body": body.optional(),
                },
                calls.review,
                effects="write",
            ),
            self._op(
                "repos.comment",
                "Comment on a pull request or an issue.",
                {"repo": repo, "number": number, "body": body},
                calls.comment,
                effects="write",
            ),
            self._op(
                "repos.openIssue",
                "Open an issue.",
                {
                    "repo": repo,
                    "title": string_schema(),
                    "body": body.optional(),
                    "labels": array_schema(string_schema()).optional(),
                },
                calls.open_issue,
                effects="write",
            ),
            self._op(
                "repos.closeIssue",
                "Close an issue, or reopen it with `reopen`.",
                {"repo": repo, "number": number, "reopen": boolean_schema().optional()},
                calls.close_issue,
                effects="write",
            ),
            self._op(
                "repos.rerun",
                "Re-run a CI job, or only its failed jobs with `failed_only`.",
                {"repo": repo, "run": string_schema(), "failed_only": boolean_schema().optional()},
                calls.rerun,
                effects="write",
            ),
            self._op(
                "repos.cancelRun",
                "Cancel a CI run that is still going.",
                {"repo": repo, "run": string_schema()},
                calls.cancel_run,
                effects="write",
            ),
            self._op(
                "repos.dispatch",
                "Start a workflow by its file name, on a branch, with optional inputs.",
                {
                    "repo": repo,
                    "workflow": string_schema().describe("The workflow file, like ci.yml."),
                    "ref": string_schema(),
                    "inputs": record_schema(string_schema()).optional(),
                },
                calls.dispatch,
                effects="write",
            ),
        )

    def _watch(self, wakes: bool) -> AnyOperation:
        return self._op(
            "repos.watch",
            "Be told when something happens on a repository, without polling: CI settles "
            "(`checks_settled`, with `number` or `ref`), a pull request merges "
            "(`pull_merged`), a review lands (`review_submitted`), or a run completes "
            "(`run_completed`, with `run`). Returns a handle at once; a notice arrives when "
            f"it happens. With `wake` (default {'true' if wakes else 'false'}) an idle "
            "conversation is woken to act on it, under the person's standing consent (watch, "
            "wait for, when, notify, until).",
            {
                "repo": repo_field("The repository, as owner/name, written out."),
                "until": enum_schema(*WATCH_KINDS),
                "number": integer_schema().min(1).optional(),
                "ref": string_schema().optional(),
                "run": string_schema().optional(),
                "objective": string_schema().describe(
                    "What this watch is for, in a sentence a person would write: it is what "
                    "the notice and the woken turn read."
                ),
                "for_seconds": number_schema()
                .optional()
                .describe(
                    "How long to keep watching. Omitted, the person's own default "
                    f"({DEFAULT_WATCH_SECONDS:.0f} unless they chose one); a week at most. "
                    "Expiry is a notice, not a failure."
                ),
                "wake": boolean_schema().optional(),
            },
            self._start_watch,
            effects="write",
        )

    # ------------------------------------------------------------------ watching

    async def _start_watch(
        self, run: RunContext[PackContext], client: ReposClient
    ) -> dict[str, Any]:
        seam = run.ctx.subscriptions
        if seam is None:
            raise ReposInputError(NO_SUBSCRIPTIONS)
        raw = dict(run.input)
        until = str(raw["until"])
        target = {
            key: raw[key] for key in ("number", "ref", "run") if raw.get(key) not in (None, "")
        }
        if not watch_target_fits(until, target):
            message = f"`until: {until}` needs {WATCH_NEEDS[until]}."
            raise ReposInputError(message)
        objective = " ".join(str(raw.get("objective") or "").split())
        if not objective:
            message = "Say what the watch is for, in a sentence."
            raise ReposInputError(message)
        lifetime = watch_seconds(
            raw.get("for_seconds"), run.ctx.defaults.get("repos.watch_seconds")
        )
        wake = raw.get("wake", True) is not False
        repo = full_name(raw)
        opened = await seam.open(
            capability=self.id, objective=objective, timeout_seconds=lifetime, wake=wake
        )
        try:
            sibling_id = await client.subscribe(
                run.ctx.profile,
                repo,
                {
                    "kind": until,
                    "target": target,
                    "expires_in_seconds": int(lifetime),
                    "signal": {"url": opened.signal_url, "secret": opened.secret},
                },
            )
        except Exception:
            seam.abandon(opened)
            raise
        await seam.attach(opened, sibling_id)
        return {
            "id": opened.handle.id,
            "state": "running",
            "for_seconds": lifetime,
            "wake": wake,
            "advice": "Watching. Carry on, or finish your answer; a notice arrives when it "
            "happens"
            + (", and the conversation is woken if nobody is talking." if wake else ".")
            + (seam.advice() if wake else ""),
        }

    async def release_subscription(self, http: Http, row: Mapping[str, Any]) -> None:
        """A person cancelled: ask the service to stop watching."""
        sibling = str(row.get("sibling_id") or "")
        if sibling:
            await self._client_on(http).unsubscribe(str(row["profile"]), sibling)

    async def check_subscription(self, http: Http, row: Mapping[str, Any]) -> Signal | None:
        """The sweep's question: has this ended, while its signal was lost?"""
        sibling = str(row.get("sibling_id") or "")
        if not sibling:
            return None
        answer = await self._client_on(http).subscription(str(row["profile"]), sibling)
        state = answer["state"]
        if state not in {"fired", "failed", "expired"}:
            return None
        facts = answer.get("facts") or {}
        return Signal(
            state=state,
            summary=str(answer.get("summary") or state),
            facts={k: v for k, v in facts.items() if isinstance(v, str | int | float | bool)},
            excerpt=str(answer.get("excerpt") or ""),
        )

    def _client(self, context: PackContext) -> ReposClient:
        return self._override or HttpReposClient(
            context.http, self.base_url, audience=self.audience
        )

    def _client_on(self, http: Http) -> ReposClient:
        return self._override or HttpReposClient(http, self.base_url, audience=self.audience)


__all__ = [
    "NOT_CONNECTED",
    "WATCH_KINDS",
    "ReposPack",
    "ReposRefusedError",
]
