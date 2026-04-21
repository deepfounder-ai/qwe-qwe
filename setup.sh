#!/bin/bash
# qwe-qwe installer — run once to set up everything
set -e

echo ""
echo "  ⚡ qwe-qwe installer"
echo "  ─────────────────────"
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
DIM='\033[2m'
NC='\033[0m'

step() { echo -e "  ${GREEN}✓${NC} $1"; }
info() { echo -e "  ${DIM}$1${NC}"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }

cd "$(dirname "$0")"

# 1. Python check
if ! command -v python3 &>/dev/null; then
    echo "  ✗ Python 3.11+ required. Install it first."
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo $PY_VER | cut -d. -f1)
PY_MINOR=$(echo $PY_VER | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]); then
    echo "  ✗ Python 3.11+ required (found $PY_VER)"
    exit 1
fi
step "Python $PY_VER"

# 2. Virtual environment
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    step "Created virtual environment"
else
    step "Virtual environment exists"
fi

source .venv/bin/activate

# 3. Install package
pip install -q --upgrade pip 2>/dev/null
pip install -q -e "." 2>/dev/null || pip install -q -r requirements.txt 2>/dev/null
step "Installed qwe-qwe + dependencies"

# 4. Verify critical dependencies
MISSING=""
python3 -c "import cryptography" 2>/dev/null || MISSING="$MISSING cryptography"
python3 -c "import openai" 2>/dev/null || MISSING="$MISSING openai"
python3 -c "from qdrant_client import QdrantClient" 2>/dev/null || MISSING="$MISSING qdrant-client"
python3 -c "from fastembed import TextEmbedding" 2>/dev/null || MISSING="$MISSING fastembed"
python3 -c "import rich" 2>/dev/null || MISSING="$MISSING rich"
python3 -c "import fastapi" 2>/dev/null || MISSING="$MISSING fastapi"
python3 -c "import uvicorn" 2>/dev/null || MISSING="$MISSING uvicorn"
python3 -c "import requests" 2>/dev/null || MISSING="$MISSING requests"
python3 -c "from PIL import Image" 2>/dev/null || MISSING="$MISSING Pillow"
python3 -c "import pypdf" 2>/dev/null || MISSING="$MISSING pypdf"
python3 -c "from markitdown import MarkItDown" 2>/dev/null || MISSING="$MISSING markitdown[all]"
python3 -c "import docx" 2>/dev/null || MISSING="$MISSING python-docx"
python3 -c "import pptx" 2>/dev/null || MISSING="$MISSING python-pptx"
python3 -c "import openpyxl" 2>/dev/null || MISSING="$MISSING openpyxl"

if [ -n "$MISSING" ]; then
    warn "Missing packages:$MISSING — installing..."
    pip install -q $MISSING 2>/dev/null
    step "Installed missing packages"
else
    step "All dependencies verified"
fi

# 5. Pre-download embedding model (first run takes ~2min otherwise)
info "Pre-loading embedding model (one-time, ~200MB)..."
if python3 -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')" 2>/dev/null; then
    step "Embedding model ready"
else
    warn "Embedding model download failed — will retry on first use"
fi

# 6. Create dirs
mkdir -p logs memory skills uploads
step "Created directories (logs/, memory/, skills/, uploads/)"

# 7. Auto-discover LLM servers
echo ""
info "Searching for LLM servers..."
LM_FOUND=false
for port in 1234 11434 8080; do
    if curl -s --connect-timeout 1 "http://localhost:$port/v1/models" >/dev/null 2>&1; then
        MODELS=$(curl -s "http://localhost:$port/v1/models" | python3 -c "import sys,json; [print(f'    - {m[\"id\"]}') for m in json.load(sys.stdin).get('data',[])]" 2>/dev/null)
        step "LLM server found at localhost:$port"
        if [ -n "$MODELS" ]; then
            echo "$MODELS"
        fi
        LM_FOUND=true
        break
    fi
done
if ! $LM_FOUND; then
    warn "No LLM server found on localhost"
    info "Start LM Studio or Ollama, load a model, then run qwe-qwe"
    info "Or set QWE_LLM_URL=http://<ip>:<port>/v1"
fi

# 8. Summary
echo ""
echo "  ─────────────────────"
echo -e "  ${GREEN}Ready!${NC}"
echo ""
echo "  Usage:"
echo "    source .venv/bin/activate"
echo ""
echo "    qwe-qwe              # terminal chat"
echo "    qwe-qwe --web        # web UI (http://localhost:7860)"
echo "    qwe-qwe --web --port 8080"
echo ""
echo "    python cli.py        # alternative: run directly"
echo "    python server.py     # alternative: web server directly"
echo ""
