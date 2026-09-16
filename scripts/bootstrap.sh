#!/usr/bin/env bash
# Idempotent family bootstrap. Check before installing. Never auto-installs Docker.
# Never prints secrets.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh [--dry-run] [--no-install] [--check] [--help]

  --dry-run      print what would happen; change nothing
  --no-install   skip `make install` in each checkout
  --check        run `make check` per checkout and print a summary table
  --help         this message

Clones missing repositories from repos.txt. An existing checkout is left alone
(no pull, fetch, reset, or checkout). Docker is reported, never installed.
EOF
}

DRY_RUN=0
NO_INSTALL=0
RUN_CHECK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-install) NO_INSTALL=1 ;;
    --check) RUN_CHECK=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "bootstrap: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
cd "$ROOT" || exit 1

# Bash 3 (macOS /bin/bash) has no associative arrays. One variable per tool.
STATUS_UV="missing"
STATUS_PYTHON="missing"
STATUS_MAKE="missing"
STATUS_GIT="missing"
STATUS_GH="missing"
STATUS_JQ="missing"
STATUS_SQLITE3="missing"
STATUS_DOCKER="missing"
STATUS_GITHUB="not signed in"

say() { printf '%s\n' "$*"; }
would() { say "dry-run: $*"; }

is_linux() {
  case "$(uname -s)" in
    Linux*) return 0 ;;
    *) return 1 ;;
  esac
}

is_darwin() {
  case "$(uname -s)" in
    Darwin*) return 0 ;;
    *) return 1 ;;
  esac
}

have() { command -v "$1" >/dev/null 2>&1; }

trim() {
  # Leading/trailing whitespace. Bash 3 compatible; no sed.
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

mark() {
  local tool="$1" status="$2"
  case "$tool" in
    uv) STATUS_UV="$status" ;;
    python) STATUS_PYTHON="$status" ;;
    make) STATUS_MAKE="$status" ;;
    git) STATUS_GIT="$status" ;;
    gh) STATUS_GH="$status" ;;
    jq) STATUS_JQ="$status" ;;
    sqlite3) STATUS_SQLITE3="$status" ;;
    docker) STATUS_DOCKER="$status" ;;
    github) STATUS_GITHUB="$status" ;;
    *) say "bootstrap: unknown tool $tool" >&2 ;;
  esac
}

install_system() {
  local binary="$1"
  local package="${2:-$1}"
  if have "$binary"; then
    mark "$binary" "already present"
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    would "install $package ($binary)"
    mark "$binary" "missing"
    return 0
  fi
  if is_darwin && have brew; then
    if brew install "$package"; then
      mark "$binary" "installed"
      return 0
    fi
  elif is_linux && have apt-get; then
    if sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends "$package"; then
      mark "$binary" "installed"
      return 0
    fi
  fi
  say "bootstrap: $binary is not installed and this OS has no automatic installer for it"
  mark "$binary" "missing"
}

ensure_uv() {
  if have uv; then
    mark "uv" "already present"
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    would "install uv (https://astral.sh/uv/install.sh)"
    mark "uv" "missing"
    return 0
  fi
  if have curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer drops the binary in ~/.local/bin; pick it up for this process.
    if [[ -x "${HOME}/.local/bin/uv" ]]; then
      PATH="${HOME}/.local/bin:${PATH}"
      export PATH
    fi
    if have uv; then
      mark "uv" "installed"
      return 0
    fi
  fi
  say "bootstrap: uv is missing and could not be installed"
  mark "uv" "missing"
}

python_ready() {
  have uv || return 1
  uv python find 3.11 >/dev/null 2>&1 || return 1
  uv python find 3.12 >/dev/null 2>&1 || return 1
  return 0
}

ensure_python() {
  if python_ready; then
    mark "python" "already present"
    return 0
  fi
  if ! have uv; then
    mark "python" "missing"
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    would "uv python install 3.11 3.12"
    mark "python" "missing"
    return 0
  fi
  if uv python install 3.11 3.12; then
    mark "python" "installed"
  else
    mark "python" "missing"
  fi
}

ensure_docker() {
  if have docker && docker info >/dev/null 2>&1; then
    mark "docker" "already present"
    return 0
  fi
  if have docker; then
    say "bootstrap: docker is on PATH but the engine is not running (not installed by this script)"
  else
    say "bootstrap: docker is not on PATH (not installed by this script)"
  fi
  mark "docker" "missing"
}

is_interactive() { [[ -t 0 && -t 1 ]]; }

ensure_github() {
  local login
  local instruction="Sign in to GitHub in your browser so bootstrap can clone the family's private repositories"
  if ! have gh; then
    say "$instruction"
    say "bootstrap: install gh, then run gh auth login --hostname github.com --git-protocol https --web"
    return 0
  fi
  if ! gh auth status --hostname github.com >/dev/null 2>&1; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      would "gh auth login --hostname github.com --git-protocol https --web (when a terminal is available)"
      would "gh auth setup-git --hostname github.com (after signing in)"
      return 0
    fi
    say "$instruction"
    if ! is_interactive; then
      say "bootstrap: no terminal; run gh auth login --hostname github.com --git-protocol https --web, then rerun bootstrap"
      return 0
    fi
    if ! gh auth login --hostname github.com --git-protocol https --web; then
      say "bootstrap: not signed in; continuing with the checkouts available to this account"
      return 0
    fi
  fi
  if login="$(gh api user --hostname github.com --jq .login 2>/dev/null)" && [[ -n "$login" ]]; then
    mark "github" "signed in as $login"
  else
    mark "github" "signed in (account name unavailable)"
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    would "gh auth setup-git --hostname github.com"
  elif ! gh auth setup-git --hostname github.com; then
    say "bootstrap: could not configure git; run gh auth setup-git --hostname github.com"
  fi
}

clone_missing() {
  local line name url
  if [[ ! -f "$ROOT/repos.txt" ]]; then
    say "bootstrap: repos.txt is missing" >&2
    return 1
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(trim "$line")"
    [[ -z "$line" ]] && continue
    name="${line%% *}"
    url="${line#* }"
    if [[ -e "$ROOT/$name" ]]; then
      say "$name: already present, leaving it alone"
      continue
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      would "git clone $url $name"
      continue
    fi
    if ! have git; then
      say "bootstrap: git is missing; cannot clone $name"
      continue
    fi
    # Authentication was handled above. Never hang a devcontainer on a git prompt.
    if ! GIT_TERMINAL_PROMPT=0 git clone "$url" "$ROOT/$name"; then
      say "$name: your account cannot see $url; ask for access, or check gh auth status"
    fi
  done < "$ROOT/repos.txt"
}

install_repos() {
  local line name
  [[ "$NO_INSTALL" -eq 1 ]] && return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(trim "$line")"
    [[ -z "$line" ]] && continue
    name="${line%% *}"
    if [[ ! -d "$ROOT/$name" ]]; then
      say "$name: not checked out, skipping make install"
      continue
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      would "make install ($name)"
      continue
    fi
    if [[ ! -f "$ROOT/$name/Makefile" ]]; then
      say "$name: no Makefile, skipping make install"
      continue
    fi
    make -C "$ROOT/$name" install
  done < "$ROOT/repos.txt"
}

check_repos() {
  local line name status
  [[ "$RUN_CHECK" -eq 1 ]] || return 0
  say ""
  say "check"
  say "-----"
  printf '%-22s  %s\n' "repo" "result"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(trim "$line")"
    [[ -z "$line" ]] && continue
    name="${line%% *}"
    if [[ "$name" == "Environments-api" ]] && ! is_linux; then
      printf '%-22s  %s\n' "$name" "needs Linux"
      continue
    fi
    if [[ ! -d "$ROOT/$name" ]]; then
      printf '%-22s  %s\n' "$name" "missing"
      continue
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%-22s  %s\n' "$name" "would make check"
      continue
    fi
    if make -C "$ROOT/$name" check; then
      status="pass"
    else
      status="FAIL"
    fi
    printf '%-22s  %s\n' "$name" "$status"
  done < "$ROOT/repos.txt"
}

print_tool_table() {
  say ""
  say "tools"
  say "-----"
  printf '%-10s  %s\n' "tool" "status"
  printf '%-10s  %s\n' "uv" "$STATUS_UV"
  printf '%-10s  %s\n' "python" "$STATUS_PYTHON"
  printf '%-10s  %s\n' "make" "$STATUS_MAKE"
  printf '%-10s  %s\n' "git" "$STATUS_GIT"
  printf '%-10s  %s\n' "gh" "$STATUS_GH"
  printf '%-10s  %s\n' "jq" "$STATUS_JQ"
  printf '%-10s  %s\n' "sqlite3" "$STATUS_SQLITE3"
  printf '%-10s  %s\n' "docker" "$STATUS_DOCKER"
  printf '%-10s  %s\n' "github" "$STATUS_GITHUB"
}

ensure_uv
ensure_python
install_system make make
install_system git git
install_system gh gh
install_system jq jq
install_system sqlite3 sqlite3
ensure_docker
ensure_github
clone_missing
install_repos
check_repos
print_tool_table

if [[ "$DRY_RUN" -eq 1 ]]; then
  say ""
  say "dry-run: nothing was changed"
fi
