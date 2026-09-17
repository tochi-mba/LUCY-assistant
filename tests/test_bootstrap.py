"""Exercise both bootstraps with fake tools; never touch the user's GitHub session."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BASH_MOCKS = r"""
log() { printf '%s\n' "$*" >> "$BOOTSTRAP_TEST_LOG"; }
uv() { return 0; }
make() { log "make $*"; log "make-prompt ${GIT_TERMINAL_PROMPT:-unset}"; }
jq() { return 0; }
sqlite3() { return 0; }
docker() { return 0; }
gh() {
  log "gh $*"
  case "$1 $2" in
    'auth status') [[ "$BOOTSTRAP_TEST_AUTH" == 'yes' || -n "${GH_TOKEN:-}" ]] ;;
    'auth login') [[ "$BOOTSTRAP_TEST_LOGIN" == 'yes' ]] ;;
    'auth setup-git') [[ "$BOOTSTRAP_TEST_SETUP" == 'yes' ]] ;;
    'api user') [[ "$BOOTSTRAP_TEST_API" == 'yes' ]] && printf '%s\n' 'test-owner' ;;
    *) return 90 ;;
  esac
}
git() {
  log "git $*"
  log "clone-prompt ${GIT_TERMINAL_PROMPT:-unset}"
  [[ "$1" == 'clone' ]] || return 91
  [[ "$2" != *'/Denied.git' ]] || return 128
  mkdir -p "$3"
}
"""

PWSH_MOCKS = r"""
function Write-TestLog([string]$Message) {
    Add-Content -LiteralPath $env:BOOTSTRAP_TEST_LOG -Value $Message
}
function uv { $global:LASTEXITCODE = 0 }
function make {
    Write-TestLog "make $args"
    Write-TestLog "make-prompt $env:GIT_TERMINAL_PROMPT"
    $global:LASTEXITCODE = 0
}
function jq { $global:LASTEXITCODE = 0 }
function sqlite3 { $global:LASTEXITCODE = 0 }
function docker { $global:LASTEXITCODE = 0 }
function gh {
    Write-TestLog "gh $args"
    $global:LASTEXITCODE = 0
    switch ("$($args[0]) $($args[1])") {
        'auth status' {
            if ($env:BOOTSTRAP_TEST_AUTH -ne 'yes' -and -not $env:GH_TOKEN) {
                $global:LASTEXITCODE = 1
            }
        }
        'auth login' { if ($env:BOOTSTRAP_TEST_LOGIN -ne 'yes') { $global:LASTEXITCODE = 1 } }
        'auth setup-git' { if ($env:BOOTSTRAP_TEST_SETUP -ne 'yes') { $global:LASTEXITCODE = 1 } }
        'api user' {
            if ($env:BOOTSTRAP_TEST_API -eq 'yes') { 'test-owner' }
            else { $global:LASTEXITCODE = 1 }
        }
        default { throw "Unexpected gh call: $args" }
    }
}
function git {
    Write-TestLog "git $args"
    Write-TestLog "clone-prompt $env:GIT_TERMINAL_PROMPT"
    if ($args[0] -ne 'clone') { throw "Unexpected git call: $args" }
    if ($args[1] -like '*/Denied.git') { $global:LASTEXITCODE = 128; return }
    New-Item -ItemType Directory -Path $args[2] -Force | Out-Null
    $global:LASTEXITCODE = 0
}
"""


def shell_binary(shell: str) -> str:
    # Windows' system32 bash is a WSL launcher, not a usable native test shell.
    if shell == "bash" and os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files"))  # noqa: SIM112 - Windows spells it this way / "Git/bin/bash.exe"
        if git_bash.is_file():
            return str(git_bash)
        pytest.skip("Git Bash is not installed")
    binary = shutil.which(shell)
    if not binary:
        pytest.skip(f"{shell} is not installed")
    return binary


@pytest.fixture(params=["bash", "pwsh"])
def bootstrap(request: pytest.FixtureRequest, tmp_path: Path):
    shell = request.param
    binary = shell_binary(shell)
    script_name = "bootstrap.sh" if shell == "bash" else "bootstrap.ps1"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "Existing").mkdir()
    (tmp_path / "Existing/Makefile").write_text("install:\n", encoding="utf-8")
    (tmp_path / "repos.txt").write_text(
        "Existing https://github.com/test-owner/Existing.git\n"
        "Denied https://github.com/test-owner/Denied.git\n"
        "Allowed https://github.com/test-owner/Allowed.git\n",
        encoding="utf-8",
    )
    log = tmp_path / "calls.log"

    def run(
        *,
        auth=True,
        login=True,
        setup=True,
        dry_run=False,
        interactive=False,
        token=False,
        api=True,
        gh=True,
    ):
        source = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        start = "\nensure_uv\n" if shell == "bash" else "\nEnsure-Uv\n"
        assert source.count(start) == 1
        overrides = ""
        if interactive:
            # A deterministic fake terminal; the rest of the script runs unchanged.
            overrides += (
                "\nis_interactive() { return 0; }\n"
                if shell == "bash"
                else "\nfunction Test-Interactive { return $true }\n"
            )
        if not gh:
            overrides += (
                '\nhave() { [[ "$1" != gh ]] && command -v "$1" >/dev/null 2>&1; }\n'
                'install_system() { mark "$1" "missing"; }\n'
                if shell == "bash"
                else "\nfunction Test-Have([string]$Name) {\n"
                '    return $Name -ne "gh" -and [bool](Get-Command $Name -ErrorAction SilentlyContinue)\n'
                '}\nfunction Install-SystemBinary([string]$Binary) { Set-Tool $Binary "missing" }\n'
            )
        source = source.replace(start, overrides + start)
        script = scripts / script_name
        script.write_text(source, encoding="utf-8", newline="\n")
        env = os.environ.copy()
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            env.pop(name, None)
        env.update(
            BOOTSTRAP_TEST_LOG=log.as_posix(),
            BOOTSTRAP_TEST_AUTH="yes" if auth else "no",
            BOOTSTRAP_TEST_LOGIN="yes" if login else "no",
            BOOTSTRAP_TEST_SETUP="yes" if setup else "no",
            BOOTSTRAP_TEST_API="yes" if api else "no",
            GH_CONFIG_DIR=str(tmp_path / "gh-config"),
            GIT_CONFIG_GLOBAL=str(tmp_path / "git-config"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="original",
        )
        if token:
            env["GH_TOKEN"] = "fake-token-never-print-this"
        if shell == "bash":
            driver = tmp_path / "driver.sh"
            driver.write_text(BASH_MOCKS + '\nsource "$1" "$2"\n', encoding="utf-8", newline="\n")
            args = [binary, str(driver), script.as_posix(), "--dry-run" if dry_run else "--check"]
        else:
            driver = tmp_path / "driver.ps1"
            driver.write_text(
                "param([string]$Script, [string]$Mode)\n"
                + PWSH_MOCKS
                + '\nif ($Mode -eq "dry") { & $Script -DryRun } else { & $Script -Check }\n',
                encoding="utf-8",
            )
            args = [
                binary,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(driver),
                str(script),
                "dry" if dry_run else "run",
            ]
        result = subprocess.run(args, env=env, input="", capture_output=True, text=True, timeout=30)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr, calls, tmp_path

    return run


def test_signed_in_account_sets_helper_before_cloning_and_reports_login(bootstrap) -> None:
    output, calls, _ = bootstrap()
    setup = next(i for i, call in enumerate(calls) if call.startswith("gh auth setup-git"))
    clone = next(i for i, call in enumerate(calls) if call.startswith("git clone"))
    assert setup < clone
    assert "signed in as test-owner" in output
    assert "github" in output
    assert not any(call.startswith("gh auth login") for call in calls)


def test_failed_clone_continues_and_existing_checkout_still_installs(bootstrap) -> None:
    output, calls, root = bootstrap()
    assert "Denied: your account cannot see https://github.com/test-owner/Denied.git" in output
    assert "gh auth status" in output
    assert (root / "Allowed").is_dir()
    assert not any("git clone https://github.com/test-owner/Existing.git" in call for call in calls)
    assert any(
        call.startswith("make -C ") and "Existing" in call and "install" in call for call in calls
    )
    assert calls.count("clone-prompt 0") == 2
    assert "make-prompt original" in calls


def test_no_terminal_does_not_prompt_or_configure_unauthenticated_helper(bootstrap) -> None:
    output, calls, root = bootstrap(auth=False)
    assert "Sign in to GitHub so bootstrap can clone" in output
    assert "not signed in" in output
    assert not any(call.startswith(("gh auth login", "gh auth setup-git")) for call in calls)
    assert (root / "Allowed").is_dir()
    assert any("Existing" in call and "install" in call for call in calls)


@pytest.mark.parametrize("auth", [True, False])
def test_dry_run_never_logs_in_changes_helper_clones_or_installs(bootstrap, auth) -> None:
    output, calls, root = bootstrap(auth=auth, dry_run=True)
    assert "dry-run: nothing was changed" in output
    assert "github" in output
    assert not any(
        call.startswith(("gh auth login", "gh auth setup-git", "git", "make")) for call in calls
    )
    assert not (root / "Allowed").exists()


def test_interactive_login_offers_browser_or_token_and_then_configures_git(bootstrap) -> None:
    output, calls, _ = bootstrap(auth=False, interactive=True)
    assert "choose the browser, or paste a token" in output
    # No --web: gh itself asks "browser or paste a token", which is the one prompt we want.
    assert "gh auth login --hostname github.com --git-protocol https" in calls
    assert any(call.startswith("gh auth setup-git") for call in calls)
    assert "signed in as test-owner" in output


def test_cancelled_login_keeps_existing_checkout_usable(bootstrap) -> None:
    output, calls, _ = bootstrap(auth=False, interactive=True, login=False)
    assert "not signed in" in output
    assert not any(call.startswith("gh auth setup-git") for call in calls)
    assert any("Existing" in call and "install" in call for call in calls)


def test_failed_helper_setup_is_actionable_and_does_not_abort(bootstrap) -> None:
    output, _, root = bootstrap(setup=False)
    assert "could not configure git" in output
    assert "gh auth setup-git" in output
    assert (root / "Allowed").is_dir()


def test_environment_token_works_without_terminal_and_is_never_printed(bootstrap) -> None:
    output, calls, _ = bootstrap(auth=False, token=True)
    assert "signed in as test-owner" in output
    assert any(call.startswith("gh auth setup-git") for call in calls)
    assert not any(call.startswith("gh auth login") for call in calls)
    assert "fake-token-never-print-this" not in output + "\n".join(calls)


def test_account_lookup_failure_does_not_discard_valid_authentication(bootstrap) -> None:
    output, calls, _ = bootstrap(api=False)
    assert "signed in (account name unavailable)" in output
    assert any(call.startswith("gh auth setup-git") for call in calls)


@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_gh_explains_signin_without_running_it(bootstrap, dry_run) -> None:
    output, calls, _ = bootstrap(gh=False, dry_run=dry_run)
    assert "install gh, then run gh auth login" in output
    assert "not signed in" in output
    assert not any(call.startswith("gh ") for call in calls)
    if not dry_run:
        assert any("Existing" in call and "install" in call for call in calls)


def test_powershell_winget_package_ids_refresh_path_and_handle_failure(tmp_path: Path) -> None:
    binary = shell_binary("pwsh")
    source = (ROOT / "scripts/bootstrap.ps1").read_text(encoding="utf-8")
    # Load the functions without the entry point, then exercise the Windows branch
    # with fake installers even when this test is running on Linux or macOS.
    functions, main = source.split("\nEnsure-Uv\n", 1)
    assert "Ensure-GitHub" in main
    driver = tmp_path / "winget.ps1"
    driver.write_text(
        functions
        + r"""
Set-Variable IsWindows -Value $true -Force -Scope Script
Set-Variable IsLinux -Value $false -Force -Scope Script
function Test-Darwin { return $false }
# The real refresh reads registry values and preserves process-only PATH entries.
$originalPath = $env:PATH
$env:PATH = 'bootstrap-test-existing-path'
Update-SystemPath
$pathPreserved = $env:PATH.StartsWith('bootstrap-test-existing-path')
foreach ($scope in @('Machine', 'User')) {
    $registered = [Environment]::GetEnvironmentVariable('Path', $scope)
    if ($registered -and -not $env:PATH.Contains([Environment]::ExpandEnvironmentVariables($registered))) {
        throw 'Registry PATH entries were not refreshed'
    }
}
$env:PATH = $originalPath
$script:available = @{}
$script:installs = [Collections.Generic.List[string]]::new()
$script:refreshes = 0
function Test-Have([string]$Name) {
    return $Name -eq 'winget' -or $script:available.ContainsKey($Name)
}
function Update-SystemPath { $script:refreshes++ }
function winget {
    $script:installs.Add("$args")
    if ($script:fail) { $global:LASTEXITCODE = 1; return }
    $script:available[$script:requested] = $true
    $global:LASTEXITCODE = 0
}
$script:fail = $false
foreach ($name in @('gh', 'git', 'jq')) {
    $script:requested = $name
    Install-SystemBinary -Binary $name | Out-Null
    if ($ToolStatus[$name] -ne 'installed') { throw "$name was not installed" }
}
# Unknown packages remain manual; a failed installer must not claim success.
Install-SystemBinary -Binary 'unmapped-binary' | Out-Null
$script:available.Remove('gh')
$script:fail = $true
Install-SystemBinary -Binary 'gh' | Out-Null
$failed = $ToolStatus['gh']
$DryRun = $true
Install-SystemBinary -Binary 'gh' | Out-Null
# Installing uv also picks up a newly registered Windows PATH.
$DryRun = $false
function powershell {
    $script:available['uv'] = $true
    $global:LASTEXITCODE = 0
}
Ensure-Uv | Out-Null
[pscustomobject]@{
    installs = @($script:installs)
    refreshes = $script:refreshes
    failed = $failed
    unknown = $ToolStatus['unmapped-binary']
    uv = $ToolStatus['uv']
    pathPreserved = $pathPreserved
} | ConvertTo-Json -Compress
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [binary, "-NoProfile", "-NonInteractive", "-File", str(driver)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert len(observed["installs"]) == 4
    for call, package in zip(
        observed["installs"],
        ["GitHub.cli", "Git.Git", "jqlang.jq", "GitHub.cli"],
        strict=True,
    ):
        assert f"install --id {package} --exact --source winget" in call
        assert "--disable-interactivity" in call
    assert observed["refreshes"] == 5
    assert observed["failed"] == observed["unknown"] == "missing"
    assert observed["uv"] == "installed"
    assert observed["pathPreserved"] is True


def test_devcontainer_forwards_token_and_retries_on_attach() -> None:
    config = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text(encoding="utf-8"))
    assert config["remoteEnv"]["GH_TOKEN"] == "${localEnv:GH_TOKEN}"
    assert config["postAttachCommand"] == "bash scripts/bootstrap.sh --no-install"


def test_bootstrap_keeps_linux_line_endings_on_windows_checkouts() -> None:
    assert b"\r" not in (ROOT / "scripts/bootstrap.sh").read_bytes()
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
