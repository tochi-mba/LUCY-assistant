#!/usr/bin/env pwsh
# Install the global client from this checkout, then run its first-run guide.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$DryRun = $false
$SkipSetup = $false
$SetupArguments = [Collections.Generic.List[string]]::new()
foreach ($Argument in $args) {
    switch -Regex ($Argument) {
        '^(--dry-run|-DryRun)$' { $DryRun = $true; continue }
        '^(--skip-setup|-SkipSetup)$' { $SkipSetup = $true; continue }
        '^(-h|--help|-Help)$' {
            @"
Usage: pwsh /path/to/LUCY-assistant/scripts/setup.ps1 [options] [lucy setup options]

  -DryRun, --dry-run         show the plan without installing or saving anything
  -SkipSetup, --skip-setup   install without launching the first-run guide
  -Help, --help             show this help

Other options are passed to lucy setup, for example:
  --mode remote --url https://lucy.example --no-token --yes
  --mode family --no-token --yes

Requires uv: https://docs.astral.sh/uv/getting-started/installation/
Tokens belong in the hidden setup prompt, LUCY_TOKEN, or --token-stdin.
This installs an editable client; keep this checkout at its current path.
"@
            exit 0
        }
        '^--token(=|$)' {
            [Console]::Error.WriteLine(
                'setup: never pass a token as a flag; use --token-stdin or the hidden prompt'
            )
            exit 2
        }
        default { $SetupArguments.Add([string]$Argument) }
    }
}

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $Root 'pyproject.toml') -PathType Leaf)) {
    [Console]::Error.WriteLine('setup: run this script from a complete LUCY-assistant checkout')
    exit 2
}
if ($DryRun) {
    $QuotedRoot = "'" + $Root.Replace("'", "''") + "'"
    Write-Output "dry-run: uv tool install --python 3.12 --editable $QuotedRoot"
    if (-not $SkipSetup) {
        Write-Output 'dry-run: launch the installed lucy setup with the supplied options'
    }
    Write-Output 'dry-run: nothing was changed'
    exit 0
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine(
        'setup: uv is missing; install it, open a new terminal, then rerun this script:'
    )
    [Console]::Error.WriteLine('https://docs.astral.sh/uv/getting-started/installation/')
    exit 2
}

# uv reuses a matching installation. Never force replacement of another executable.
& uv tool install --python 3.12 --editable $Root
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine(
        'setup: installation failed; review the uv error and uv tool list, then rerun'
    )
    exit 1
}
Write-Output 'Installed lucy. If your shell cannot find it, run uv tool update-shell and open a new terminal.'
if ($SkipSetup) { exit 0 }

# Use uv's executable directory so first run works before a PATH refresh.
$BinDirectory = & uv tool dir --bin
if ($LASTEXITCODE -ne 0 -or -not $BinDirectory) {
    [Console]::Error.WriteLine('setup: cannot locate the installed command; check uv tool list')
    exit 1
}
$LucyCommand = Join-Path "$BinDirectory" 'lucy'
if (-not (Test-Path -LiteralPath $LucyCommand -PathType Leaf)) { $LucyCommand += '.exe' }
if (-not (Test-Path -LiteralPath $LucyCommand -PathType Leaf)) {
    [Console]::Error.WriteLine('setup: the installed lucy command was not found; check uv tool list')
    exit 1
}
& $LucyCommand setup @SetupArguments
exit $LASTEXITCODE
