"""Run the installers with fake uv and lucy commands; never install or save credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH_DRIVER = r"""
log() { printf '%s\0' "$@" >> "$SETUP_TEST_LOG"; printf '\n' >> "$SETUP_TEST_LOG"; }
uv() {
  log uv "$@"
  if [[ "$1 $2" == 'tool install' ]]; then return "$SETUP_TEST_INSTALL_EXIT"; fi
  if [[ "$1 $2 $3" == 'tool dir --bin' ]]; then printf '%s\n' "$SETUP_TEST_BIN"; return 0; fi
  return 99
}
command() {
  if [[ "$1" == '-v' && "$2" == uv && "$SETUP_TEST_MISSING_UV" == 1 ]]; then return 1; fi
  builtin command "$@"
}
exec() { log "$@"; exit "$SETUP_TEST_LUCY_EXIT"; }
source "$@"
"""
PWSH_DRIVER = r"""
function Write-TestLog([string[]]$Values) {
    [IO.File]::AppendAllText($env:SETUP_TEST_LOG, ($Values -join "`0") + "`0`n")
}
function uv {
    Write-TestLog (@('uv') + $args)
    $global:LASTEXITCODE = 0
    if ("$($args[0]) $($args[1])" -eq 'tool install') {
        $global:LASTEXITCODE = [int]$env:SETUP_TEST_INSTALL_EXIT
    } elseif ("$args" -eq 'tool dir --bin') { $env:SETUP_TEST_BIN }
    else { throw 'Unexpected uv invocation' }
}
function Get-Command {
    if ($env:SETUP_TEST_MISSING_UV -eq '1') { return $null }
    Microsoft.PowerShell.Core\Get-Command @args
}
$FakeLucy = Join-Path $env:SETUP_TEST_BIN 'lucy.exe'
Set-Item -LiteralPath "Function:\$FakeLucy" -Value {
    Write-TestLog (@('lucy') + $args)
    $global:LASTEXITCODE = [int]$env:SETUP_TEST_LUCY_EXIT
}
$ScriptPath = $args[0]
$Forwarded = @($args | Select-Object -Skip 1)
& $ScriptPath @Forwarded
exit $LASTEXITCODE
"""


def shell_binary(shell: str) -> str:
    if shell == "bash" and os.name == "nt":
        git_root = os.environ.get("ProgramFiles", "C:/Program Files")  # noqa: SIM112
        binary = Path(git_root) / "Git/bin/bash.exe"
        if binary.is_file():
            return str(binary)
        pytest.skip("Git Bash is not installed")
    binary_name = shutil.which(shell)
    if binary_name is None:
        pytest.skip(f"{shell} is not installed")
    return binary_name


@pytest.fixture(params=["bash", "pwsh"])
def installer(request: pytest.FixtureRequest, tmp_path: Path):
    shell = request.param
    executable = shell_binary(shell)
    repository = tmp_path / "Lucy's checkout with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    (repository / "pyproject.toml").write_text("[project]\nname='lucy-api'\n", encoding="utf-8")
    script_name = "setup.sh" if shell == "bash" else "setup.ps1"
    script = scripts / script_name
    script.write_bytes((ROOT / "scripts" / script_name).read_bytes())
    bin_dir = tmp_path / "tool bin"
    bin_dir.mkdir()
    fake_lucy = bin_dir / ("lucy" if shell == "bash" else "lucy.exe")
    fake_lucy.write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
    fake_lucy.chmod(0o700)
    work = tmp_path / "unrelated directory"
    work.mkdir()
    log = tmp_path / "calls.bin"
    driver = tmp_path / ("driver.sh" if shell == "bash" else "driver.ps1")
    driver.write_text(
        BASH_DRIVER if shell == "bash" else PWSH_DRIVER, encoding="utf-8", newline="\n"
    )

    def run(*options: str, install_exit=0, lucy_exit=0, missing_uv=False):
        log.unlink(missing_ok=True)
        env = os.environ.copy()
        env.update(
            SETUP_TEST_LOG=log.as_posix(),
            SETUP_TEST_BIN=str(bin_dir),
            SETUP_TEST_INSTALL_EXIT=str(install_exit),
            SETUP_TEST_LUCY_EXIT=str(lucy_exit),
            SETUP_TEST_MISSING_UV="1" if missing_uv else "0",
        )
        if shell == "bash":
            # Git Bash accepts C:/ paths and returns /c/ paths; native uv accepts either.
            env["SETUP_TEST_BIN"] = bin_dir.as_posix()
            command = [executable, str(driver), script.as_posix(), *options]
        else:
            command = [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(driver),
                str(script),
                *options,
            ]
        result = subprocess.run(
            command, cwd=work, env=env, capture_output=True, text=True, timeout=30
        )
        calls = (
            [row.decode("utf-8").split("\0")[:-1] for row in log.read_bytes().splitlines()]
            if log.exists()
            else []
        )
        return result, calls

    return run


def test_installer_resolves_checkout_and_forwards_arguments_without_reparsing(installer) -> None:
    url = "https://lucy.example/some path?x=1&y=two"
    result, calls = installer("--mode", "remote", "--url", url, "--no-token", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0][:-1] == ["uv", "tool", "install", "--python", "3.12", "--editable"]
    assert calls[0][-1].endswith("Lucy's checkout with spaces")
    assert calls[1] == ["uv", "tool", "dir", "--bin"]
    assert calls[2][1:] == ["setup", "--mode", "remote", "--url", url, "--no-token", "--yes"]
    assert "uv tool update-shell" in result.stdout


def test_repeated_dry_run_is_identical_and_never_calls_install_or_setup(installer) -> None:
    first, calls = installer("--dry-run", "--mode", "family")
    second, second_calls = installer("--dry-run", "--mode", "family")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert "nothing was changed" in first.stdout
    assert "launch the installed lucy setup" in first.stdout
    assert calls == second_calls == []


def test_skip_setup_installs_without_changing_configuration(installer) -> None:
    result, calls = installer("--skip-setup")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 1
    assert "--force" not in calls[0]


def test_install_failure_never_runs_setup(installer) -> None:
    result, calls = installer(install_exit=7)
    assert result.returncode == 1
    assert len(calls) == 1
    assert "installation failed" in result.stderr
    assert "Installed lucy" not in result.stdout


def test_setup_exit_code_reaches_the_caller(installer) -> None:
    result, _ = installer(lucy_exit=3)
    assert result.returncode == 3


def test_missing_uv_has_an_actionable_error_without_installing(installer) -> None:
    result, calls = installer(missing_uv=True)
    assert result.returncode == 2
    assert "uv is missing" in result.stderr
    assert "https://docs.astral.sh/uv/getting-started/installation/" in result.stderr
    assert calls == []


@pytest.mark.parametrize("options", [("--token=do-not-print",), ("--token", "do-not-print")])
def test_token_flags_are_rejected_without_echoing_the_secret(installer, options) -> None:
    result, calls = installer("--dry-run", *options)
    assert result.returncode == 2
    assert "do-not-print" not in result.stdout + result.stderr
    assert "--token-stdin" in result.stderr
    assert calls == []


def test_help_needs_no_install_or_setup(installer) -> None:
    result, calls = installer("--help", missing_uv=True)
    assert result.returncode == 0
    assert "editable client" in result.stdout
    assert calls == []
