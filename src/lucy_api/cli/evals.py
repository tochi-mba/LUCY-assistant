"""`lucy eval`: hold the constant list of conversations against a running hub, on demand.

``lucy eval list`` reads the scenario files and shows them; it needs no hub. ``lucy eval
run`` holds each one as a real conversation with the model it is given and writes a report.
It is never run by CI or by ``make check`` -- it spends somebody's model budget and needs a
hub that is up -- which is why it refuses a hub that is not on this machine unless told
otherwise, checks the model is usable before creating anything, and says exactly how many
conversations it is about to hold before it holds the first one.

Exit codes follow the ``lucy`` contract, with ``1`` kept for the one answer this command
exists to give: ``0`` every check passed; ``1`` a check failed, or a scenario could not be
held; ``2`` the command, a scenario file, the token or the model spec was wrong, so nothing
was run; ``3`` the hub could not be reached; ``130`` Ctrl-C, after writing the report so far.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from lucy_api import __version__
from lucy_api.cli.base import OK, REFUSED, TIMEOUT_SECONDS, USAGE, CliError, unreachable
from lucy_api.evals.compare import CompareError, compare, load_previous
from lucy_api.evals.conversation import Pace
from lucy_api.evals.hub import HubError, HubUnreachable
from lucy_api.evals.loader import DEFAULT_SUITE, ScenarioError, load_suite, shipped_suites
from lucy_api.evals.markdown import LABELS, render
from lucy_api.evals.report import JSON_NAME, build_report, write_report
from lucy_api.evals.results import ERROR, FAILED
from lucy_api.evals.runner import DEFAULT_TIMEOUT, Plan, Runner, unmet

if TYPE_CHECKING:
    from lucy_api.cli.base import Context
    from lucy_api.evals.hub import Hub
    from lucy_api.evals.results import ScenarioRecord, TurnRecord
    from lucy_api.evals.runner import Job
    from lucy_api.evals.scenario import Scenario, Suite

REPORTS = Path("var") / "evals"
STAMP = "%Y%m%dT%H%M%SZ"
READ_SECONDS = 120.0
"""How long one request may take to answer: an invoke can run a sandbox command."""

USABLE_SECTIONS = ("ready", "available")
MODEL_SECTIONS = (*USABLE_SECTIONS, "unavailable")
BOLD = "1"
INTERRUPTED = "interrupted with Ctrl-C"

DESCRIPTION = """\
Hold a constant list of real conversations with Lucy, through a real model, and check
what the hub recorded after every turn. Run it on demand against a hub that is already
running; nothing in CI or `make check` ever does."""

EPILOG = """\
examples:
  lucy eval list                              every shipped scenario
  lucy eval run --model clyde:haiku           the default suite, weakest model first
  lucy eval run --model clyde:haiku --model clyde:sonnet   the same, two models side by side
  lucy eval run --model clyde:haiku --repeat 3              flaky checks show as pass rates
  lucy eval run --model clyde:haiku --compare var/evals/20260924T101500Z
  lucy eval run --model clyde:haiku --suite ./my-scenarios --dry-run

docs: docs/evals.md"""

_SUITE_HELP = "a shipped suite, or a folder of .toml files; repeatable"

RUN_EPILOG = """\
exit codes:
  0 every check passed      1 a check failed, or a scenario could not be held
  2 nothing was run: a bad flag, scenario file, token or model spec
  3 the hub could not be reached      130 Ctrl-C (the report so far is still written)"""


def add_parser(sub: Any, after: argparse.ArgumentParser) -> None:
    """``lucy eval``, ``lucy eval list`` and ``lucy eval run``."""
    evals = sub.add_parser(
        "eval",
        parents=[after],
        help="hold the regression conversations against a running hub",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    evals.set_defaults(run=cmd_eval, eval_help=evals.format_help)
    actions = evals.add_subparsers(dest="eval_command", metavar="<action>")

    listing = actions.add_parser(
        "list", parents=[after], help="the scenarios: name, turns, tags and what each guards"
    )
    listing.add_argument("--suite", action="append", metavar="NAME|PATH", help=_SUITE_HELP)

    run = actions.add_parser(
        "run",
        parents=[after],
        help="hold the conversations and write a report",
        description=DESCRIPTION,
        epilog=RUN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument(
        "--model",
        action="append",
        required=True,
        metavar="SPEC",
        help="provider:model to hold every conversation with; repeat it to compare models",
    )
    run.add_argument(
        "--suite", action="append", metavar="NAME|PATH", help=f"{_SUITE_HELP} (default: default)"
    )
    run.add_argument(
        "--scenario", action="append", metavar="NAME", help="only this one (name or suite/name)"
    )
    run.add_argument("--tag", action="append", metavar="TAG", help="only scenarios with this tag")
    run.add_argument(
        "--profile",
        metavar="P",
        help="the profile each session runs as (default: a new one for this run, eval-<time>)",
    )
    run.add_argument(
        "--repeat", type=_count, default=1, metavar="N", help="hold each conversation N times"
    )
    run.add_argument(
        "--timeout",
        type=_seconds,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"cancel a turn that has not come to rest by then (default: {DEFAULT_TIMEOUT:.0f})",
    )
    run.add_argument(
        "--report-dir", type=Path, metavar="DIR", help="default: var/evals/<UTC time>/"
    )
    run.add_argument(
        "--compare",
        type=Path,
        metavar="REPORT",
        help="a previous report.json or its folder: list regressions and fixes",
    )
    run.add_argument(
        "--keep-sessions", action="store_true", help="leave each session open, to read it later"
    )
    run.add_argument(
        "--allow-remote", action="store_true", help="allow a hub that is not on this machine"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="check everything and print the plan; create nothing"
    )


def cmd_eval(ctx: Context) -> int:
    """List the scenarios, hold them, or say how to."""
    action = getattr(ctx.args, "eval_command", None)
    if action == "list":
        return _list(ctx)
    if action == "run":
        return _run(ctx)
    ctx.out.write(ctx.args.eval_help())
    return OK


def utc_now() -> datetime:
    """Now, in UTC. A seam, so a test can say when a run happened."""
    return datetime.now(UTC)


def pace() -> Pace:
    """How a run waits between polls: the wall clock. A seam, so a test never sleeps."""
    return Pace()


# --------------------------------------------------------------------------------------
# lucy eval list
# --------------------------------------------------------------------------------------


def _list(ctx: Context) -> int:
    suites = _load(ctx.args.suite or list(shipped_suites()))
    lines: list[str] = []
    for suite in suites:
        prompts = sum(len(scenario.turns) for scenario in suite.scenarios)
        heading = f"{suite.name}: {len(suite.scenarios)} scenario(s), {prompts} prompt(s)"
        lines.append(ctx.style(heading, BOLD) + ctx.style.dim(f"  ({suite.origin})"))
        width = max(len(scenario.name) for scenario in suite.scenarios)
        for scenario in suite.scenarios:
            tags = ", ".join(scenario.tags) or "-"
            lines.append(f"  {scenario.name:<{width}}  {len(scenario.turns)} turn(s)  [{tags}]")
            lines.append(f"  {'':<{width}}  {ctx.style.dim(_first_sentence(scenario.summary))}")
        lines.append("")
    payload = {"suites": [_suite_payload(suite) for suite in suites]}
    ctx.emit(payload, "\n".join(lines).rstrip())
    return OK


def _suite_payload(suite: Suite) -> dict[str, Any]:
    return {
        "name": suite.name,
        "origin": suite.origin,
        "scenarios": [
            {
                "name": scenario.name,
                "qualified": scenario.qualified,
                "summary": scenario.summary,
                "tags": list(scenario.tags),
                "turns": len(scenario.turns),
                "requires": list(scenario.requires),
                "path": scenario.path,
            }
            for scenario in suite.scenarios
        ],
    }


def _first_sentence(text: str) -> str:
    head, _, _ = " ".join(text.split()).partition(". ")
    return head.rstrip(".") + "."


# --------------------------------------------------------------------------------------
# lucy eval run
# --------------------------------------------------------------------------------------


def _run(ctx: Context) -> int:
    args = ctx.args
    suites = _load(args.suite or [DEFAULT_SUITE])
    plan = Plan(
        scenarios=_select(suites, args.scenario or [], args.tag or []),
        models=_models(args.model),
        repeat=args.repeat,
        profile=_run_profile(args.profile),
        timeout=args.timeout,
        keep_sessions=args.keep_sessions,
    )
    previous = _previous(args.compare)
    _require_loopback(ctx.url, allowed=args.allow_remote)
    if not ctx.token:
        message = "not signed in"
        raise CliError(message, USAGE, hint="run `lucy setup`, or set LUCY_TOKEN")
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    from lucy_api.cli.evals_hub import HttpHub  # noqa: PLC0415 - it imports httpx

    with httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS, read=READ_SECONDS)) as client:
        hub = HttpHub(client, ctx.url, ctx.token)
        hub_info = _preflight(ctx, hub, plan)
        if args.dry_run:
            return _dry_run(ctx, plan, hub_info)
        directory = _report_dir(args.report_dir)
        selection = {
            "suites": list(args.suite or [DEFAULT_SUITE]),
            "scenarios": list(args.scenario or []),
            "tags": list(args.tag or []),
        }
        held = _Held(
            ctx,
            plan=plan,
            hub_info=hub_info,
            directory=directory,
            selection=selection,
            previous=previous,
        )
        return held.run(hub)


def _load(references: list[str]) -> list[Suite]:
    suites: list[Suite] = []
    for reference in references:
        try:
            suite = load_suite(reference)
        except ScenarioError as exc:
            hint = "docs/evals.md lists every key a scenario file takes"
            raise CliError(str(exc), USAGE, hint=hint) from exc
        if any(existing.name == suite.name for existing in suites):
            message = f"two suites are called {suite.name}"
            raise CliError(message, USAGE, hint="name each suite once")
        suites.append(suite)
    return suites


def _select(suites: list[Suite], names: list[str], tags: list[str]) -> tuple[Scenario, ...]:
    """The scenarios asked for, in suite and file order, whatever order they were named in."""
    pool = [scenario for suite in suites for scenario in suite.scenarios]
    if names:
        chosen: set[str] = set()
        for name in names:
            found = [scenario for scenario in pool if name in {scenario.name, scenario.qualified}]
            if not found:
                message = f"no scenario called {name!r}"
                raise CliError(message, USAGE, hint="`lucy eval list` shows every scenario")
            if len(found) > 1:
                both = ", ".join(scenario.qualified for scenario in found)
                message = f"{name!r} is in more than one suite: {both}"
                raise CliError(message, USAGE, hint="name it as suite/name")
            chosen.add(found[0].qualified)
        pool = [scenario for scenario in pool if scenario.qualified in chosen]
    if tags:
        pool = [scenario for scenario in pool if set(scenario.tags) & set(tags)]
    if not pool:
        message = "no scenario matches that selection"
        raise CliError(message, USAGE, hint="`lucy eval list` shows every scenario and its tags")
    return tuple(pool)


def _models(specs: list[str]) -> tuple[str, ...]:
    models: list[str] = []
    for raw in specs:
        spec = raw.strip()
        provider, separator, model = spec.partition(":")
        if not separator or not provider.strip() or not model.strip():
            message = f"{raw!r} is not a model spec"
            hint = "write provider:model, for example clyde:haiku; `lucy models` lists providers"
            raise CliError(message, USAGE, hint=hint)
        if spec in models:
            message = f"{spec} is named twice"
            raise CliError(message, USAGE, hint="name each model once")
        models.append(spec)
    return tuple(models)


def _previous(path: Path | None) -> dict[str, Any] | None:
    """Read before the run, so a mistyped path costs a second, not a whole run."""
    if path is None:
        return None
    try:
        return load_previous(path)
    except CompareError as exc:
        raise CliError(str(exc), USAGE, hint="--compare takes a report.json or its folder") from exc


def _require_loopback(url: str, *, allowed: bool) -> None:
    """A run creates sessions and spends a model budget; somebody else's is not ours to spend."""
    if allowed or _is_loopback(urlsplit(url).hostname or ""):
        return
    message = f"{url} is not this machine"
    hint = "an eval creates sessions and spends that hub's model budget; add --allow-remote"
    raise CliError(message, USAGE, hint=hint)


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _preflight(ctx: Context, hub: Hub, plan: Plan) -> dict[str, Any]:
    """Everything that can be known without creating anything: version, models, readiness."""
    try:
        health = hub.health()
        warnings = _usable(hub.models(), plan.models)
        requiring = any(scenario.requires for scenario in plan.scenarios)
        capabilities = hub.capabilities(plan.profile) if requiring else []
    except HubError as exc:
        raise _refusal(ctx, exc) from exc
    for warning in warnings:
        ctx.say(ctx.style.warn(f"warning: {warning}"))
    reasons = {
        scenario.qualified: unmet(scenario, capabilities)
        for scenario in plan.scenarios
        if scenario.requires
    }
    skips = {name: reason for name, reason in reasons.items() if reason}
    return {
        "url": ctx.url,
        "version": health.get("version"),
        "environment": health.get("environment"),
        "skips": skips,
    }


def _refusal(ctx: Context, exc: HubError) -> CliError:
    if isinstance(exc, HubUnreachable):
        return unreachable(ctx.url, exc.__cause__ or exc)
    if exc.fatal:
        message = "the hub refused the token"
        return CliError(message, USAGE, hint="run `lucy setup --force` with a current token")
    message = f"the hub could not answer before the run: {exc}"
    return CliError(message, USAGE, hint="run `lucy doctor`")


def _usable(listing: dict[str, Any], models: tuple[str, ...]) -> list[str]:
    """Refuse a spec the hub cannot use, naming what it can. Returns softer doubts."""
    rows = {
        str(row.get("provider")): (section, row)
        for section in MODEL_SECTIONS
        for row in listing.get(section) or []
        if isinstance(row, dict)
    }
    usable = {
        provider: row for provider, (section, row) in rows.items() if section in USABLE_SECTIONS
    }
    offer = ", ".join(
        f"{provider}:{model}"
        for provider, row in usable.items()
        for model in (row.get("models") or ["<model>"])
    )
    warnings: list[str] = []
    for spec in models:
        provider, _, model = spec.partition(":")
        if provider in usable:
            listed = [str(item) for item in usable[provider].get("models") or []]
            if usable[provider].get("local") and listed and model not in listed:
                warnings.append(f"{spec}: {provider} lists {', '.join(listed)}, not {model}")
            continue
        hint = f"usable on this hub: {offer}" if offer else "nothing is usable; run `lucy models`"
        if provider in rows:
            section, row = rows[provider]
            message = f"{spec} is not usable on this hub: {provider} is {section}"
            detail = str(row.get("detail") or "")
            raise CliError(f"{message}: {detail}" if detail else message, USAGE, hint=hint)
        message = f"{spec}: this hub has no model provider called {provider!r}"
        raise CliError(message, USAGE, hint=hint)
    return warnings


def _dry_run(ctx: Context, plan: Plan, hub_info: dict[str, Any]) -> int:
    rows = [
        {
            "scenario": scenario.qualified,
            "turns": len(scenario.turns),
            "seeds": len(scenario.seed),
            "requires": list(scenario.requires),
            "skip": hub_info["skips"].get(scenario.qualified) or None,
        }
        for scenario in plan.scenarios
    ]
    payload = {
        "dry_run": True,
        "hub": {key: hub_info[key] for key in ("url", "version", "environment")},
        "models": list(plan.models),
        "profile": plan.profile,
        "repeat": plan.repeat,
        "conversations": len(plan.jobs()),
        "prompts": plan.prompts,
        "scenarios": rows,
    }
    width = max(len(scenario.qualified) for scenario in plan.scenarios)
    lines = [f"Would hold {_plan_line(plan, hub_info)}.", ""]
    for scenario in plan.scenarios:
        reason = hub_info["skips"].get(scenario.qualified)
        skip = f"  would skip: {reason}" if reason else ""
        lines.append(f"  {scenario.qualified:<{width}}  {len(scenario.turns)} turn(s){skip}")
    lines.extend(["", "Nothing was created: this was a dry run."])
    ctx.emit(payload, "\n".join(lines))
    return OK


def _plan_line(plan: Plan, hub_info: dict[str, Any]) -> str:
    jobs = len(plan.jobs())
    return (
        f"{len(plan.scenarios)} scenario(s) x {len(plan.models)} model(s) x {plan.repeat} "
        f"run(s) = {jobs} conversation(s), {plan.prompts} prompt(s), with "
        f"{', '.join(plan.models)} on {hub_info['url']} (hub {hub_info['version'] or 'unknown'}) "
        f"as profile {plan.profile}"
    )


def _run_profile(chosen: str | None) -> str:
    """The profile a run holds its conversations in: one of its own, unless one was named.

    Runs used to share the person's own profile, and so their memories. A scenario that asks
    Lucy to remember something found it "already" remembered -- from the run before, or from
    the person -- and failed for the wrong reason; and the person found "I prefer tea over
    coffee" among their own notes. A profile per run reads nothing it did not write.
    """
    return chosen or f"eval-{utc_now().strftime(STAMP).lower()}"


def _report_dir(chosen: Path | None) -> Path:
    """A fresh folder for this run. A report is never written over another one."""
    if chosen is not None:
        if (chosen / JSON_NAME).exists():
            message = f"{chosen} already holds a report"
            raise CliError(message, USAGE, hint="choose a new --report-dir")
        target = chosen
    else:
        stamp = utc_now().strftime(STAMP)
        target = REPORTS / stamp
        suffix = 1
        while target.exists():
            suffix += 1
            target = REPORTS / f"{stamp}-{suffix}"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        message = f"cannot create {target}: {exc.strerror or exc}"
        raise CliError(message, USAGE, hint="choose a --report-dir you can write to") from exc
    return target


class _Held:
    """One run in progress, with everything it needs to write its report however it ends."""

    def __init__(  # noqa: PLR0913 - everything a report is written from
        self,
        ctx: Context,
        *,
        plan: Plan,
        hub_info: dict[str, Any],
        directory: Path,
        selection: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> None:
        self._ctx = ctx
        self._plan = plan
        self._hub_info = hub_info
        self._directory = directory
        self._selection = selection
        self._previous = previous
        self._records: list[ScenarioRecord] = []

    def run(self, hub: Hub) -> int:
        ctx = self._ctx
        ctx.say(f"Holding {_plan_line(self._plan, self._hub_info)}.")
        ctx.say(f"The report will be in {self._directory}")
        started = utc_now()
        runner = Runner(hub, pace=pace(), observer=Progress(ctx, self._plan))
        try:
            stopped = runner.run(self._plan, self._records.append)
        except KeyboardInterrupt:
            self._write(started, INTERRUPTED)
            raise
        report, paths = self._write(started, str(stopped) if stopped else "")
        ctx.emit(_outcome_payload(report, paths), _outcome_text(ctx, report, paths))
        if isinstance(stopped, HubUnreachable):
            raise unreachable(ctx.url, stopped.__cause__ or stopped)
        if stopped is not None:
            message = f"the run stopped early: {stopped}"
            raise CliError(message, USAGE, hint="run `lucy setup --force` with a current token")
        return OK if report["passed"] else REFUSED

    def _write(self, started: datetime, stopped: str) -> tuple[dict[str, Any], tuple[Path, Path]]:
        environment = {
            "hub": {key: self._hub_info[key] for key in ("url", "version", "environment")},
            "client": {
                "version": __version__,
                "python": platform.python_version(),
                "platform": sys.platform,
            },
        }
        report = build_report(
            plan=self._plan,
            records=self._records,
            environment=environment,
            started=started,
            finished=utc_now(),
            stopped=stopped,
            selection=self._selection,
        )
        if self._previous is not None:
            label = str(self._ctx.args.compare)
            report["comparison"] = compare(self._previous, report, label=label)
        return report, write_report(self._directory, report, markdown=render(report))


class Progress:
    """Each scenario and turn as it lands, on stderr, so stdout keeps only the answer."""

    def __init__(self, ctx: Context, plan: Plan) -> None:
        self._ctx = ctx
        self._plan = plan

    def started(self, job: Job, number: int, total: int) -> None:
        run = f", run {job.repeat}" if self._plan.repeat > 1 else ""
        self._ctx.say(f"[{number}/{total}] {job.scenario.qualified} with {job.model}{run}")

    def turn_finished(self, job: Job, turn: TurnRecord) -> None:
        del job
        style = self._ctx.style
        mark = style.good("ok") if turn.passed else style.bad("FAIL")
        self._ctx.say(
            f"    turn {turn.index}: {turn.status or 'unknown'} in {turn.seconds:.1f}s, "
            f"{turn.iterations} round(s), {len(turn.results)} step(s)  {mark}"
        )

    def finished(self, record: ScenarioRecord) -> None:
        style = self._ctx.style
        label = LABELS[record.outcome]
        shown = style.bad(label) if record.outcome in {FAILED, ERROR} else style.good(label)
        reason = f": {record.reason}" if record.reason else ""
        self._ctx.say(f"    {shown}{reason}")


def _failures(report: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for run in report["runs"]:
        if run["outcome"] not in {FAILED, ERROR}:
            continue
        failing = [
            check["name"]
            for turn in run["turns"]
            for check in turn["checks"]
            if not check["passed"]
        ]
        found.append(
            {
                "scenario": run["scenario"],
                "model": run["model"],
                "repeat": run["repeat"],
                "outcome": run["outcome"],
                "reason": run["reason"],
                "failing_checks": failing,
            }
        )
    return found


def _outcome_payload(report: dict[str, Any], paths: tuple[Path, Path]) -> dict[str, Any]:
    return {
        "passed": report["passed"],
        "stopped": report["stopped"],
        "summary": report["summary"],
        "failures": _failures(report),
        "comparison": report["comparison"],
        "report": {"json": str(paths[0]), "markdown": str(paths[1])},
    }


def _outcome_text(ctx: Context, report: dict[str, Any], paths: tuple[Path, Path]) -> str:
    lines = [
        f"{model}: {row['passed']} passed, {row['failed']} failed, {row['error']} error(s), "
        f"{row['skipped']} skipped; {row['checks_passed']}/{row['checks_total']} checks held"
        for model, row in report["summary"].items()
    ]
    for failure in _failures(report):
        why = failure["failing_checks"][0] if failure["failing_checks"] else failure["reason"]
        more = len(failure["failing_checks"]) - 1
        extra = f" (and {more} more)" if more > 0 else ""
        lines.append(
            f"  {ctx.style.bad(LABELS[failure['outcome']])} {failure['scenario']} with "
            f"{failure['model']}, run {failure['repeat']}: {why}{extra}"
        )
    comparison = report["comparison"]
    if comparison is not None:
        lines.append(
            f"compared with {comparison['previous']}: {len(comparison['regressions'])} "
            f"regression(s), {len(comparison['fixes'])} fix(es)"
        )
        lines.extend(
            f"  regressed: {row['scenario']} with {row['model']} "
            f"({row['before']} -> {row['after']})"
            for row in comparison["regressions"]
        )
    lines.append(f"report: {paths[1]}")
    return "\n".join(lines)


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        message = f"{text!r} is not a whole number"
        raise argparse.ArgumentTypeError(message) from exc
    if value < 1:
        message = "must be 1 or more"
        raise argparse.ArgumentTypeError(message)
    return value


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        message = f"{text!r} is not a number of seconds"
        raise argparse.ArgumentTypeError(message) from exc
    if not math.isfinite(value) or value <= 0:
        message = "must be more than zero seconds"
        raise argparse.ArgumentTypeError(message)
    return value


__all__ = ["Progress", "add_parser", "cmd_eval", "pace", "utc_now"]
