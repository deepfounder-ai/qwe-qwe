#!/bin/bash
# qwe-qwe one-line installer
# curl -fsSL https://raw.githubusercontent.com/anthropic-lab/qwe-qwe/main/install.sh | bash
set -e

# ── branding ──────────────────────────────────────────────
BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

echo ""
echo -e "  ${YELLOW}   ____                  ____                 ${NC}"
echo -e "  ${YELLOW}  / __ \\__      _____   / __ \\__      _____  ${NC}"
echo -e "  ${YELLOW} | |  | \\ \\ /\\ / / _ \\ | |  | \\ \\ /\\ / / _ \\ ${NC}"
echo -e "  ${YELLOW} | |__| |\\ V  V /  __/ | |__| |\\ V  V /  __/ ${NC}"
echo -e "  ${YELLOW}  \\___\\_\\ \\_/\\_/ \\___|  \\___\\_\\ \\_/\\_/ \\___| ${NC}"
echo ""
echo -e "  ${DIM}Lightweight offline AI agent for local models${NC}"
echo -e "  ${DIM}https://github.com/deepfounder-ai/qwe-qwe${NC}"
echo ""

step()  { echo -e "  ${GREEN}✓${NC} $1"; }
info()  { echo -e "  ${DIM}  $1${NC}"; }
warn()  { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail()  { echo -e "  ${RED}✗${NC} $1"; exit 1; }

# ── config ────────────────────────────────────────────────
REPO="https://github.com/deepfounder-ai/qwe-qwe.git"
INSTALL_DIR="${QWE_INSTALL_DIR:-$HOME/qwe-qwe}"
BRANCH="main"

# ── preflight ─────────────────────────────────────────────

# git
command -v git &>/dev/null || fail "git is required. Install it first."

# python 3.11+
command -v python3 &>/dev/null || fail "Python 3.11+ is required. Install it first."
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fail "Python 3.11+ required (found $PY_VER)"
fi
step "Python $PY_VER"

# ── clone or update ───────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "  ${DIM}Updating existing installation...${NC}"
    cd "$INSTALL_DIR"
    git fetch --quiet origin
    git reset --quiet --hard "origin/$BRANCH"
    step "Updated to latest"
else
    if [ -d "$INSTALL_DIR" ]; then
        warn "$INSTALL_DIR exists but is not a git repo"
        warn "Move or remove it, then re-run the installer"
        exit 1
    fi
    git clone --quiet --depth 1 -b "$BRANCH" "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    step "Cloned to $INSTALL_DIR"
fi

# ── venv + install ────────────────────────────────────────
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    step "Created virtual environment"
else
    step "Virtual environment exists"
fi

source .venv/bin/activate
pip install -q --upgrade pip 2>/dev/null
pip install -q -e "." 2>&1 | tail -1 || pip install -q -r requirements.txt 2>/dev/null
step "Installed qwe-qwe + dependencies"

# ── verify deps ──────────────────────────────────────────
MISSING=""
python3 -c "import cryptography" 2>/dev/null || MISSING="$MISSING cryptography"
python3 -c "from fastembed import TextEmbedding" 2>/dev/null || MISSING="$MISSING fastembed"
python3 -c "from qdrant_client import QdrantClient" 2>/dev/null || MISSING="$MISSING qdrant-client"
python3 -c "import openai" 2>/dev/null || MISSING="$MISSING openai"
python3 -c "import rich" 2>/dev/null || MISSING="$MISSING rich"
python3 -c "import fastapi" 2>/dev/null || MISSING="$MISSING fastapi"
python3 -c "import requests" 2>/dev/null || MISSING="$MISSING requests"
python3 -c "from markitdown import MarkItDown" 2>/dev/null || MISSING="$MISSING markitdown[all]"
python3 -c "import docx" 2>/dev/null || MISSING="$MISSING python-docx"
python3 -c "import pptx" 2>/dev/null || MISSING="$MISSING python-pptx"
python3 -c "import openpyxl" 2>/dev/null || MISSING="$MISSING openpyxl"
python3 -c "import pypdf" 2>/dev/null || MISSING="$MISSING pypdf"
if [ -n "$MISSING" ]; then
    warn "Missing:$MISSING — installing..."
    pip install -q $MISSING 2>/dev/null
fi
step "Dependencies verified"

# ── pre-download embedding model ─────────────────────────
info "Pre-loading embedding model (~200MB, one-time)..."
python3 -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')" 2>/dev/null \
    && step "Embedding model ready" \
    || warn "Embedding model will download on first use"

# ── dirs ──────────────────────────────────────────────────
mkdir -p logs memory skills uploads
step "Created directories"

# ── shell integration ─────────────────────────────────────
SHELL_NAME=$(basename "$SHELL" 2>/dev/null || echo "bash")
BIN_DIR="$INSTALL_DIR/.venv/bin"

add_to_path() {
    local rc_file="$1"
    local marker="# qwe-qwe PATH"
    if [ -f "$rc_file" ] && grep -q "$marker" "$rc_file" 2>/dev/null; then
        return 0
    fi
    echo "" >> "$rc_file"
    echo "$marker" >> "$rc_file"
    echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$rc_file"
    return 1
}

ADDED_PATH=false
case "$SHELL_NAME" in
    zsh)  add_to_path "$HOME/.zshrc"   && true || ADDED_PATH=true ;;
    fish) mkdir -p "$HOME/.config/fish"
          FISH_CONF="$HOME/.config/fish/config.fish"
          if ! grep -q "qwe-qwe" "$FISH_CONF" 2>/dev/null; then
              echo "" >> "$FISH_CONF"
              echo "# qwe-qwe PATH" >> "$FISH_CONF"
              echo "set -gx PATH $BIN_DIR \$PATH" >> "$FISH_CONF"
              ADDED_PATH=true
          fi ;;
    *)    add_to_path "$HOME/.bashrc"   && true || ADDED_PATH=true ;;
esac

if $ADDED_PATH; then
    step "Added qwe-qwe to PATH"
else
    step "PATH already configured"
fi

# Make qwe-qwe available immediately in this session
export PATH="$BIN_DIR:$PATH"

# ── LM Studio / Ollama auto-discovery ─────────────────────
echo ""
info "Searching for LLM servers..."
LM_FOUND=false
for port in 1234 11434 8080; do
    if curl -s --connect-timeout 1 "http://localhost:$port/v1/models" >/dev/null 2>&1; then
        step "LLM server found at localhost:$port"
        LM_FOUND=true
        break
    fi
done
if ! $LM_FOUND; then
    # Try env override
    if [ -n "${LM_STUDIO_HOST:-}" ]; then
        if curl -s --connect-timeout 2 "http://${LM_STUDIO_HOST}/v1/models" >/dev/null 2>&1; then
            step "LLM server reachable at $LM_STUDIO_HOST"
            LM_FOUND=true
        fi
    fi
fi
if ! $LM_FOUND; then
    warn "No LLM server found"
    info "Start LM Studio or Ollama, load a model, then run qwe-qwe"
    info "Or set QWE_LLM_URL=http://<ip>:<port>/v1"
fi

# ── done ──────────────────────────────────────────────────
echo ""
echo -e "  ──────────────────────────────────────"
echo -e "  ${GREEN}${BOLD}⚡ qwe-qwe installed!${NC}"
echo ""
if $ADDED_PATH; then
    echo -e "  Restart your shell or run:"
    echo -e "    ${DIM}source ~/.${SHELL_NAME}rc${NC}"
    echo ""
fi
echo -e "  Quick start:"
echo -e "    ${BOLD}qwe-qwe${NC}              # terminal chat"
echo -e "    ${BOLD}qwe-qwe --web${NC}        # web UI"
echo -e "    ${BOLD}qwe-qwe --doctor${NC}     # verify setup"
echo ""
echo -e "  ${DIM}If 'qwe-qwe' is not found, run:${NC}"
echo -e "    ${DIM}${INSTALL_DIR}/.venv/bin/qwe-qwe --web${NC}"
echo ""
echo -e "  ${DIM}Docs: https://github.com/deepfounder-ai/qwe-qwe${NC}"
echo ""
