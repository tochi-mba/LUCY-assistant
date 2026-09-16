#!/usr/bin/env pwsh
# Idempotent family bootstrap. Check before installing. Never auto-installs Docker.
# Never prints secrets.
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoInstall,
    [switch]$Check,
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
# Native commands (uv python find, docker info) returning non-zero must not abort.
$PSNativeCommandUseErrorActionPreference = $false

function Show-Usage {
    @"
Usage: pwsh scripts/bootstrap.ps1 [-DryRun] [-NoInstall] [-Check] [-Help]

  -DryRun      print what would happen; change nothing
  -NoInstall   skip ``make install`` in each checkout
  -Check       run ``make check`` per checkout and print a summary table
  -Help        this message

Clones missing repositories from repos.txt. An existing checkout is left alone
(no pull, fetch, reset, or checkout). Docker is reported, never installed.
"@
}

if ($Help) {
    Show-Usage
    exit 0
}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$ToolStatus = [ordered]@{}

function Write-Say([string]$Message) {
    Write-Output $Message
}

function Write-Would([string]$Message) {
    Write-Say "dry-run: $Message"
}

function Test-Have([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-Linux {
    if ($IsLinux) { return $true }
    try {
        $os = (uname -s 2>$null)
        return "$os" -like "Linux*"
    } catch {
        return $false
    }
}

function Test-Darwin {
    return [bool]$IsMacOS
}

function Set-Tool([string]$Tool, [string]$Status) {
    $ToolStatus[$Tool] = $Status
}

function Install-SystemBinary {
    param(
        [Parameter(Mandatory = $true)][string]$Binary,
        [string]$Package = $Binary
    )
    if (Test-Have $Binary) {
        Set-Tool $Binary "already present"
        return
    }
    if ($DryRun) {
        Write-Would "install $Package ($Binary)"
        Set-Tool $Binary "missing"
        return
    }
    if ((Test-Darwin) -and (Test-Have "brew")) {
        & brew install $Package
        if (Test-Have $Binary) {
            Set-Tool $Binary "installed"
            return
        }
    } elseif ($IsLinux -and (Test-Have "apt-get")) {
        & sudo apt-get update -qq
        & sudo apt-get install -y --no-install-recommends $Package
        if (Test-Have $Binary) {
            Set-Tool $Binary "installed"
            return
        }
    }
    Write-Say "bootstrap: $Binary is not installed and this OS has no automatic installer for it"
    Set-Tool $Binary "missing"
}

function Ensure-Uv {
    if (Test-Have "uv") {
        Set-Tool "uv" "already present"
        return
    }
    if ($DryRun) {
        Write-Would "install uv"
        Set-Tool "uv" "missing"
        return
    }
    if ($IsWindows) {
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    } elseif (Test-Have "curl") {
        curl -LsSf https://astral.sh/uv/install.sh | sh
        $local = Join-Path $HOME ".local/bin"
        if (Test-Path $local) {
            $env:PATH = "$local$([IO.Path]::PathSeparator)$env:PATH"
        }
    }
    if (Test-Have "uv") {
        Set-Tool "uv" "installed"
    } else {
        Write-Say "bootstrap: uv is missing and could not be installed"
        Set-Tool "uv" "missing"
    }
}

function Test-PythonReady {
    if (-not (Test-Have "uv")) { return $false }
    & uv python find 3.11 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    & uv python find 3.12 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Ensure-Python {
    if (Test-PythonReady) {
        Set-Tool "python" "already present"
        return
    }
    if (-not (Test-Have "uv")) {
        Set-Tool "python" "missing"
        return
    }
    if ($DryRun) {
        Write-Would "uv python install 3.11 3.12"
        Set-Tool "python" "missing"
        return
    }
    & uv python install 3.11 3.12
    if (Test-PythonReady) {
        Set-Tool "python" "installed"
    } else {
        Set-Tool "python" "missing"
    }
}

function Ensure-Docker {
    if (Test-Have "docker") {
        $engine = $true
        try {
            & docker info 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { $engine = $false }
        } catch {
            $engine = $false
        }
        if ($engine) {
            Set-Tool "docker" "already present"
            return
        }
        Write-Say "bootstrap: docker is on PATH but the engine is not running (not installed by this script)"
        Set-Tool "docker" "missing"
        return
    }
    Write-Say "bootstrap: docker is not on PATH (not installed by this script)"
    Set-Tool "docker" "missing"
}

function Get-RepoRows {
    $path = Join-Path $Root "repos.txt"
    if (-not (Test-Path $path)) {
        throw "repos.txt is missing"
    }
    Get-Content -Path $path | ForEach-Object {
        $raw = $_
        $line = ($raw -split "#", 2)[0].Trim()
        if ($line) {
            $parts = $line.Split(" ", 2, [StringSplitOptions]::RemoveEmptyEntries)
            [pscustomobject]@{ Name = $parts[0]; Url = $(if ($parts.Count -gt 1) { $parts[1] } else { "" }) }
        }
    }
}

function Clone-Missing {
    foreach ($row in Get-RepoRows) {
        $dest = Join-Path $Root $row.Name
        if (Test-Path $dest) {
            Write-Say "$($row.Name): already present, leaving it alone"
            continue
        }
        if ($DryRun) {
            Write-Would "git clone $($row.Url) $($row.Name)"
            continue
        }
        if (-not (Test-Have "git")) {
            Write-Say "bootstrap: git is missing; cannot clone $($row.Name)"
            continue
        }
        & git clone $row.Url $dest
    }
}

function Install-Repos {
    if ($NoInstall) { return }
    foreach ($row in Get-RepoRows) {
        $dest = Join-Path $Root $row.Name
        if (-not (Test-Path $dest)) {
            Write-Say "$($row.Name): not checked out, skipping make install"
            continue
        }
        if ($DryRun) {
            Write-Would "make install ($($row.Name))"
            continue
        }
        if (-not (Test-Path (Join-Path $dest "Makefile"))) {
            Write-Say "$($row.Name): no Makefile, skipping make install"
            continue
        }
        & make -C $dest install
    }
}

function Check-Repos {
    if (-not $Check) { return }
    Write-Say ""
    Write-Say "check"
    Write-Say "-----"
    Write-Output ("{0,-22}  {1}" -f "repo", "result")
    foreach ($row in Get-RepoRows) {
        $name = $row.Name
        if ($name -eq "Environments-api" -and -not (Test-Linux)) {
            Write-Output ("{0,-22}  {1}" -f $name, "needs Linux")
            continue
        }
        $dest = Join-Path $Root $name
        if (-not (Test-Path $dest)) {
            Write-Output ("{0,-22}  {1}" -f $name, "missing")
            continue
        }
        if ($DryRun) {
            Write-Output ("{0,-22}  {1}" -f $name, "would make check")
            continue
        }
        & make -C $dest check
        $status = if ($LASTEXITCODE -eq 0) { "pass" } else { "FAIL" }
        Write-Output ("{0,-22}  {1}" -f $name, $status)
    }
}

function Write-ToolTable {
    Write-Say ""
    Write-Say "tools"
    Write-Say "-----"
    Write-Output ("{0,-10}  {1}" -f "tool", "status")
    foreach ($tool in @("uv", "python", "make", "git", "gh", "jq", "sqlite3", "docker")) {
        $status = if ($ToolStatus.Contains($tool)) { $ToolStatus[$tool] } else { "missing" }
        Write-Output ("{0,-10}  {1}" -f $tool, $status)
    }
}

Ensure-Uv
Ensure-Python
Install-SystemBinary -Binary "make"
Install-SystemBinary -Binary "git"
Install-SystemBinary -Binary "gh"
Install-SystemBinary -Binary "jq"
Install-SystemBinary -Binary "sqlite3"
Ensure-Docker
Clone-Missing
Install-Repos
Check-Repos
Write-ToolTable

if ($DryRun) {
    Write-Say ""
    Write-Say "dry-run: nothing was changed"
}
