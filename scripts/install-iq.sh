#!/usr/bin/env bash
# ============================================================================
# hermes-agent-IQ — minimal installer (macOS / Linux)
# ============================================================================
# Clones the fork, sets up uv + a Python 3.11 venv, installs deps, and prints
# next steps. For the full-featured installer (Termux, root/FHS layout, Node,
# Playwright, etc.) see scripts/install.sh.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/almaghrabima/hermes-agent-IQ/main/scripts/install-iq.sh | bash
#
# Options (pass after `bash -s --`):
#   --dir PATH       install location (default: ~/.hermes/hermes-agent-IQ)
#   --branch NAME    git branch to install (default: main)
#   --extras LIST    pip extras to install (default: all; e.g. "all,dev")
#   -h, --help       show this help
# ============================================================================
set -euo pipefail

REPO_URL="https://github.com/almaghrabima/hermes-agent-IQ.git"
BRANCH="main"
EXTRAS="all"
INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/.hermes/hermes-agent-IQ}"

# --- pretty output (no color when not a tty) ---------------------------------
if [ -t 1 ]; then B='\033[1m'; G='\033[0;32m'; Y='\033[0;33m'; R='\033[0;31m'; N='\033[0m'; else B=''; G=''; Y=''; R=''; N=''; fi
info() { printf "${G}==>${N} %s\n" "$*"; }
warn() { printf "${Y}warning:${N} %s\n" "$*"; }
die()  { printf "${R}error:${N} %s\n" "$*" >&2; exit 1; }

# --- args --------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)    INSTALL_DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --extras) EXTRAS="$2"; shift 2 ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# --- environment hygiene (avoid module shadowing when run from a tool) -------
unset PYTHONPATH PYTHONHOME 2>/dev/null || true
export UV_NO_CONFIG=1

# --- prerequisites -----------------------------------------------------------
case "$(uname -s)" in
  Darwin|Linux) ;;
  *) die "unsupported OS '$(uname -s)'. Use scripts/install.ps1 on Windows." ;;
esac
command -v git  >/dev/null 2>&1 || die "git is required but not found."
command -v curl >/dev/null 2>&1 || die "curl is required but not found."

# --- uv (Python toolchain manager) ------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  info "Installing uv (Astral)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv lands in ~/.local/bin (or legacy ~/.cargo/bin); make it visible this session.
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && export PATH="$d:$PATH"
  done
  command -v uv >/dev/null 2>&1 || die "uv install succeeded but uv is not on PATH; reopen your shell and re-run."
fi

# --- clone or update ---------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  info "Updating existing checkout at $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
  info "Cloning $REPO_URL (branch $BRANCH) into $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# --- venv + dependencies -----------------------------------------------------
if [ -d .venv ]; then
  info "Reusing existing virtualenv (.venv)"
else
  info "Creating Python 3.11 virtualenv (.venv) via uv"
  uv venv .venv --python 3.11
fi
# shellcheck disable=SC1091
source .venv/bin/activate
info "Installing hermes-agent[$EXTRAS] (editable) — this can take a few minutes"
uv pip install -e ".[$EXTRAS]"

# --- done --------------------------------------------------------------------
HERMES_BIN="$INSTALL_DIR/.venv/bin/hermes"
printf "\n${B}${G}hermes-agent-IQ installed.${N}\n\n"
cat <<EOF
Next steps:
  1. Activate the environment:
       source "$INSTALL_DIR/.venv/bin/activate"
  2. Run the setup wizard:
       hermes setup
  3. Start a conversation:
       hermes

The 'hermes' command is at:
  $HERMES_BIN

Optional: the 'rlm' (recursive-language-model) tool needs Deno —
  curl -fsSL https://deno.land/install.sh | sh
EOF
