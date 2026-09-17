"""Find the family checkout and run the shared GitHub App install from it.

The published family CI app is installed in the browser. Nothing here creates, reads,
writes, or prints a GitHub token or an Actions secret; ``scripts/connect_github.py`` is the
same flow ``make github-ci`` has always run.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_api.cli.base import FAMILY_ROOT_VAR, USAGE, CliError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from lucy_api.cli.base import Context

MARKERS = ("family-app.json", "repos.txt")
# src/lucy_api/cli/family.py → family checkout when this tree is an editable install.
PACKAGE_ROOT = Path(__file__).resolve().parents[3]


def is_family_root(path: Path) -> bool:
    """A family desk is the meta-repo: the app metadata and the public clone list."""
    return all((path / name).is_file() for name in MARKERS)


def find_family_root(
    *,
    cwd: Path,
    environ: Mapping[str, str],
    origin: Path | None = None,
) -> Path | None:
    """``LUCY_FAMILY_ROOT``, then this directory and its parents, then the installed tree."""
    explicit = environ.get(FAMILY_ROOT_VAR, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path.resolve() if is_family_root(path) else None
    starts = [cwd.resolve()]
    if origin is not None:
        starts.append(origin.resolve())
    seen: set[Path] = set()
    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in seen:
                continue
            seen.add(candidate)
            if is_family_root(candidate):
                return candidate
    return None


def load_connect(root: Path) -> Any:
    """Load the standalone installer that ``make github-ci`` already runs."""
    script = root / "scripts" / "connect_github.py"
    if not script.is_file():
        msg = "this family checkout is missing scripts/connect_github.py"
        raise CliError(msg, USAGE, hint="run lucy setup from a complete LUCY-assistant checkout")
    spec = importlib.util.spec_from_file_location("lucy_connect_github", script)
    if spec is None or spec.loader is None:
        msg = "cannot load the family GitHub App installer"
        raise CliError(msg, USAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_github_ci(
    ctx: Context,
    root: Path,
    *,
    connect: Callable[..., int] | None = None,
) -> int:
    """Open the family CI app's Install page for this checkout's repositories."""
    installer = connect if connect is not None else load_connect(root).connect
    interactive = bool(ctx.interactive and not ctx.args.yes)
    return int(
        installer(
            repos_file=root / "repos.txt",
            app_file=root / "family-app.json",
            dry_run=bool(ctx.args.dry_run),
            interactive=interactive,
            force=bool(ctx.args.github_ci),
        )
    )


def should_install_github_app(ctx: Context, mode: str) -> bool:
    """Family mode installs https://github.com/apps/lucy-assistant-family-ci by default."""
    if ctx.args.no_github_ci:
        return False
    if ctx.args.github_ci:
        return True
    return mode == "family"


APP_PAGE = "https://github.com/apps/lucy-assistant-family-ci"


def github_app_state(root: Path, *, run: Any | None = None) -> dict[str, Any]:
    """Whether lucy-assistant-family-ci is already installed on this family's owner."""
    module = load_connect(root)
    try:
        owner, _names = module.read_family(root / "repos.txt")
        slug, _ = module.read_app(root / "family-app.json")
    except module.ConnectError as exc:
        return {"state": "unknown", "done": False, "detail": str(exc), "page": APP_PAGE}
    runner = run or subprocess.run
    signed_in = module.run_tool(runner, ("gh", "auth", "status", "--hostname", "github.com"))
    if signed_in.returncode != 0:
        return {
            "state": "signed_out",
            "done": False,
            "detail": "not signed in to GitHub",
            "page": APP_PAGE,
            "slug": slug,
        }
    found = module.find_installation(owner, slug, run=runner)
    if found:
        return {
            "state": "installed",
            "done": True,
            "detail": f"{APP_PAGE} on {owner}",
            "page": APP_PAGE,
            "account": owner,
            "slug": slug,
        }
    return {
        "state": "missing",
        "done": False,
        "detail": f"not installed on {owner}",
        "page": APP_PAGE,
        "account": owner,
        "slug": slug,
    }


def extra_desk(root: Path) -> dict[str, Any]:
    """Operator-local extras: clone list, compose override, token grants, checkouts."""
    local = root / ".repos.local.txt"
    if not local.is_file():
        local = root / "repos.local.txt"
    names: list[str] = []
    if local.is_file():
        for raw in local.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                names.append(line.split()[0])
    checkouts = []
    for name in names:
        at_root = root / name
        nested = root / "private" / name
        checkouts.append(
            {
                "name": name,
                "path": str(at_root if at_root.is_dir() else nested if nested.is_dir() else ""),
                "present": at_root.is_dir() or nested.is_dir(),
            }
        )
    return {
        "manifest": local.is_file(),
        "compose_override": (root / "docker-compose.override.yml").is_file(),
        "genenv": (root / "scripts" / "genenv.local.json").is_file(),
        "checkouts": checkouts,
    }


def github_ci_notice(code: int, *, dry_run: bool, already: bool = False) -> str:
    if already:
        return f"Already installed: {APP_PAGE}"
    if dry_run:
        return f"Would open {APP_PAGE} in the browser so you can install it on this family."
    if code == 0:
        return f"Opened {APP_PAGE} in the browser. Choose Only select repositories for this family."
    return (
        f"Could not complete the {APP_PAGE} install from here. "
        "Run python scripts/connect_github.py from the family checkout, or make github-ci."
    )


def checkout_hint() -> str:
    return (
        "Family GitHub App install needs a LUCY-assistant checkout "
        f"(repos.txt and family-app.json). Set {FAMILY_ROOT_VAR}, or run setup from that directory."
    )
