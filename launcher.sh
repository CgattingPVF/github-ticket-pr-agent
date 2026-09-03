#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────
# Ticket PR Agent / MergeQuest — single-command launcher
# Usage:  ./launcher.sh
#         ./launcher.sh --help
#         ./launcher.sh --no-browser
# Works on: Linux, macOS, WSL (bash)
# What it does:
#   1. Verifies python3 (>=3.10) exists
#   2. Creates .venv if missing/corrupt (via python3 -m venv)
#   3. Installs/updates Python deps from requirements.txt
#   4. Creates .env from .env.example if absent
#   5. Ensures data/ and workspaces/ directories exist
#   6. Starts the Flask app (launcher.py → browser, fallback app.py)
# ─────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
REQUIREMENTS="requirements.txt"
ENV_EXAMPLE=".env.example"
ENV_FILE=".env"
PYTHON_BIN="python3"
NO_BROWSER=0
PORT_OVERRIDE=""

# ——— argument parsing ———
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      cat <<'EOF'
Usage: ./launcher.sh [options]

Options:
  --help, -h        Show this help and exit
  --no-browser      Don't auto-open a browser tab (still prints the URL)
  --port PORT       Override start port (default: 3060 via APP_PORT or auto-detect)
  --reinstall       Force reinstall of Python dependencies
  --verbose         Show pip install output

Environment overrides:
  PYTHON=python3.11   Use a specific python interpreter
  VENV_DIR=.venv      Override venv location
  APP_PORT=3060       Override Flask port (also via --port)
  APP_HOST=127.0.0.1  Override Flask host

Examples:
  ./launcher.sh
  ./launcher.sh --no-browser --port 4000
  PYTHON=python3.12 ./launcher.sh --verbose
EOF
      exit 0
      ;;
    --no-browser)
      NO_BROWSER=1
      shift
      ;;
    --reinstall)
      REINSTALL=1
      shift
      ;;
    --verbose)
      VERBOSE=1
      shift
      ;;
    --port)
      if [[ $# -lt 2 || "$2" == --* ]]; then
        echo "error: --port requires a value (e.g. --port 3060)" >&2
        exit 2
      fi
      PORT_OVERRIDE="$2"
      shift 2
      ;;
    --port=*)
      PORT_OVERRIDE="${1#--port=}"
      if [[ -z "$PORT_OVERRIDE" ]]; then
        echo "error: --port requires a value (e.g. --port=3060)" >&2
        exit 2
      fi
      shift
      ;;
    --*)
      echo "Unknown option: $1 (try --help)" >&2
      exit 2
      ;;
    *)
      echo "Unknown argument: $1 (try --help)" >&2
      exit 2
      ;;
  esac
done

REINSTALL="${REINSTALL:-0}"
VERBOSE="${VERBOSE:-0}"
PYTHON_BIN="${PYTHON:-$PYTHON_BIN}"
VENV_DIR="${VENV_DIR:-.venv}"

# ——— helpers ———
info()  { printf "\033[1;34m[launcher]\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m[launcher]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[launcher]\033[0m %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[launcher] error:\033[0m %s\n" "$*" >&2; exit 1; }

PIP_QUIET=(-q)
if [[ "$VERBOSE" == "1" ]]; then
  PIP_QUIET=()
fi

# ——— 1. python check ———
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  die "$PYTHON_BIN not found. Install Python 3.10+ and ensure '$PYTHON_BIN' is on PATH.
  Hint: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")' 2>&1)"
PY_MAJOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')"
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
  die "Python $PY_VERSION found, but Python 3.10+ is required. Found: $("$PYTHON_BIN" --version 2>&1)"
fi
info "Using $("$PYTHON_BIN" --version 2>&1) at $(command -v "$PYTHON_BIN")"

# ——— 2. ensure venv ———
NEED_VENV=0
if [[ ! -d "$VENV_DIR" ]]; then
  NEED_VENV=1
elif [[ ! -x "$VENV_DIR/bin/python" ]]; then
  warn "$VENV_DIR exists but $VENV_DIR/bin/python is missing/broken — recreating venv"
  NEED_VENV=1
elif [[ "$REINSTALL" == "1" ]]; then
  info "--reinstall requested — will reinstall deps (venv kept)"
fi

if [[ "$NEED_VENV" == "1" ]]; then
  info "Creating virtual environment at $VENV_DIR ..."
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR" 2>&1; then
    die "Failed to create venv. On Debian/Ubuntu you may need: sudo apt install -y python3-venv
  Command was: $PYTHON_BIN -m venv $VENV_DIR"
  fi
  ok "Virtual environment created"
else
  info "Virtual environment found at $VENV_DIR"
fi

# shellcheck disable=SC1090
if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
  die "$VENV_DIR/bin/activate not found — venv is corrupt. Remove $VENV_DIR and re-run."
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
info "Activated venv: $VIRTUAL_ENV"

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

if [[ ! -x "$VENV_PYTHON" ]]; then
  die "Venv python not executable at $VENV_PYTHON"
fi

# ——— 3. install deps ———
if [[ ! -f "$REQUIREMENTS" ]]; then
  die "$REQUIREMENTS not found in $SCRIPT_DIR"
fi

# upgrade pip only if needed; keep output quiet unless --verbose
info "Syncing Python dependencies from $REQUIREMENTS ..."
if ! "$VENV_PIP" install --upgrade pip "${PIP_QUIET[@]}" 2>&1; then
  warn "pip upgrade failed — continuing anyway"
fi
if ! "$VENV_PIP" install -r "$REQUIREMENTS" "${PIP_QUIET[@]}" 2>&1; then
  die "pip install failed. Try: $VENV_PIP install -r $REQUIREMENTS --verbose"
fi
ok "Dependencies ready ($("$VENV_PYTHON" -c 'import flask, sys; print(flask.__version__)' 2>/dev/null || echo 'Flask installed'))"

# quick import smoke test
if ! "$VENV_PYTHON" -c "import flask, authlib, dotenv, openpyxl, requests" 2>&1; then
  warn "Some imports still failing after pip install — see error above"
fi
if ! "$VENV_PYTHON" -c "import app; print('app import ok')" 2>&1; then
  die "App import check failed — see error above (missing dep or syntax error in app.py)"
fi

# ——— 4. ensure .env ———
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ENV_EXAMPLE" ]]; then
    info "$ENV_FILE not found — creating from $ENV_EXAMPLE"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    # generate a random SECRET_KEY if placeholder
    if command -v openssl >/dev/null 2>&1; then
      RAND_KEY="$(openssl rand -hex 32 2>/dev/null || true)"
      if [[ -n "$RAND_KEY" ]] && grep -q "replace-this-with" "$ENV_FILE" 2>/dev/null; then
        # replace placeholder on Linux (GNU sed) and macOS (BSD sed)
        if sed --version >/dev/null 2>&1; then
          sed -i "s/replace-this-with-a-random-value/$RAND_KEY/" "$ENV_FILE"
        else
          sed -i '' "s/replace-this-with-a-random-value/$RAND_KEY/" "$ENV_FILE"
        fi
        info "Generated random SECRET_KEY in $ENV_FILE"
      fi
    else
      warn "openssl not found — $ENV_FILE still contains placeholder SECRET_KEY. Replace it for production."
    fi
    ok "Created $ENV_FILE — review and set GITHUB_CLIENT_ID/SECRET if you use OAuth"
  else
    warn "$ENV_EXAMPLE not found — starting without $ENV_FILE (defaults will be used)"
  fi
else
  info "Using existing $ENV_FILE"
fi

# ——— 5. ensure dirs ———
"$VENV_PYTHON" -c "from config import Settings; s=Settings(); s.ensure_directories(); print(f'data: {s.data_dir}'); print(f'workspaces: {s.workspace_root}')" 2>&1 | while IFS= read -r line; do info "$line"; done

# ——— 6. preflight checks (non-fatal) ———
if ! command -v gh >/dev/null 2>&1; then
  warn "GitHub CLI 'gh' not found on PATH — the app will still start, but ticket operations require it."
  warn "  Install: https://cli.github.com/  then run: gh auth login"
else
  info "Found $(gh --version 2>&1 | head -n1)"
  if ! gh auth status >/dev/null 2>&1; then
    warn "'gh' is installed but not authenticated. Run: gh auth login"
    warn "  (or set GH_TOKEN in .env / environment)"
  else
    ok "gh auth OK"
  fi
fi
if ! command -v git >/dev/null 2>&1; then
  warn "git not found — required for cloning/branch operations"
else
  info "Found $(git --version 2>&1)"
fi

# ——— 7. port / host handling ———
if [[ -n "$PORT_OVERRIDE" ]]; then
  export APP_PORT="$PORT_OVERRIDE"
  info "Port override: APP_PORT=$APP_PORT (from --port)"
fi

APP_HOST_VAL="${APP_HOST:-127.0.0.1}"
APP_PORT_VAL="${APP_PORT:-3060}"
# if NO_BROWSER, tell launcher.py to skip? launcher.py doesn't support that natively,
# so we set an env that our wrapper respects below.
if [[ "$NO_BROWSER" == "1" ]]; then
  export MERGEQUEST_NO_BROWSER=1
  info "Browser auto-open disabled (--no-browser)"
fi

# trap SIGINT/SIGTERM to give a clean message
cleanup() {
  echo ""
  warn "Shutting down..."
}
trap cleanup INT TERM

# ——— 8. launch ———
# Prefer launcher.py (opens browser, finds free port). Fall back to app.py.
LAUNCH_TARGET=""
if [[ -f "launcher.py" ]]; then
  LAUNCH_TARGET="launcher.py"
elif [[ -f "app.py" ]]; then
  LAUNCH_TARGET="app.py"
else
  die "Neither launcher.py nor app.py found in $SCRIPT_DIR"
fi

if [[ "$LAUNCH_TARGET" == "launcher.py" ]]; then
  if [[ "$NO_BROWSER" == "1" ]]; then
    info "Starting Ticket PR Agent via launcher.py (browser suppressed) ..."
  else
    info "Starting Ticket PR Agent via launcher.py ..."
  fi
else
  info "Starting Ticket PR Agent via app.py ..."
fi
echo ""
ok "Project: $SCRIPT_DIR"
ok "Venv:    $VENV_DIR ($VENV_PYTHON)"
ok "Env:     $ENV_FILE"
if [[ -n "${APP_PORT:-}" ]]; then
  ok "Port:    ${APP_PORT} (set via APP_PORT/--port; launcher will auto-detect free port from there)"
else
  ok "Port:    auto-detect from 3060 upward"
fi
ok "Host:    ${APP_HOST:-127.0.0.1}"
echo ""
if [[ "$NO_BROWSER" == "1" ]]; then
  info "Browser auto-open disabled (--no-browser)"
fi
info "Open http://${APP_HOST_VAL}:${APP_PORT_VAL} (or the port printed below) in your browser"
info "Press Ctrl+C to stop"
echo "────────────────────────────────────────────────────────"
exec "$VENV_PYTHON" "$LAUNCH_TARGET"
