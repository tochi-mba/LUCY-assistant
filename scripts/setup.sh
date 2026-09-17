#!/usr/bin/env bash
# Install the global client from this checkout, then run its first-run guide.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash /path/to/LUCY-assistant/scripts/setup.sh [options] [lucy setup options]

  --dry-run       show the plan without installing or saving anything
  --skip-setup    install the command without launching the guide
  -h, --help      show this help

Other options are passed to lucy setup, for example:
  --mode remote --url https://lucy.example --no-token --yes
  --mode family --no-token --yes

Requires uv: https://docs.astral.sh/uv/getting-started/installation/
Tokens belong in the hidden setup prompt, LUCY_TOKEN, or --token-stdin.
This installs an editable client; keep this checkout at its current path.
EOF
}

DRY_RUN=0
SKIP_SETUP=0
SETUP_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-setup) SKIP_SETUP=1 ;;
    -h|--help) usage; exit 0 ;;
    --token|--token=*)
      printf '%s\n' 'setup: never pass a token as a flag; use --token-stdin or the hidden prompt' >&2
      exit 2
      ;;
    *) SETUP_ARGS+=("$1") ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ ! -f "$ROOT/pyproject.toml" ]]; then
  printf '%s\n' 'setup: run this script from a complete LUCY-assistant checkout' >&2
  exit 2
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'dry-run: uv tool install --python 3.12 --editable %q\n' "$ROOT"
  if [[ "$SKIP_SETUP" -eq 0 ]]; then
    printf '%s\n' 'dry-run: launch the installed lucy setup with the supplied options'
  fi
  printf '%s\n' 'dry-run: nothing was changed'
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'setup: uv is missing; install it, open a new terminal, then rerun this script:' \
    'https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 2
fi

# uv reuses a matching installation. Never force replacement of another executable.
if ! uv tool install --python 3.12 --editable "$ROOT"; then
  printf '%s\n' 'setup: installation failed; review the uv error and uv tool list, then rerun' >&2
  exit 1
fi
printf '%s\n' 'Installed lucy. If your shell cannot find it, run uv tool update-shell and open a new terminal.'
if [[ "$SKIP_SETUP" -eq 1 ]]; then
  exit 0
fi

# Use uv's executable directory so first run works before a PATH refresh.
BIN_DIR="$(uv tool dir --bin)"
LUCY_COMMAND="$BIN_DIR/lucy"
if [[ ! -x "$LUCY_COMMAND" && -x "$LUCY_COMMAND.exe" ]]; then
  LUCY_COMMAND="$LUCY_COMMAND.exe"
fi
if [[ ! -x "$LUCY_COMMAND" ]]; then
  printf '%s\n' 'setup: the installed lucy command was not found; check uv tool list' >&2
  exit 1
fi
exec "$LUCY_COMMAND" setup "${SETUP_ARGS[@]}"
