@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo.
echo   qwe-qwe installer (Windows)
echo   ───────────────────────────
echo.

cd /d "%~dp0"

:: 1. Find Python 3.11+
set "PY="
for %%P in (python python3 py) do (
    where %%P >nul 2>&1 && (
        for /f "tokens=*" %%V in ('%%P -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}') if v.major>=3 and v.minor>=11 else print('old')" 2^>nul') do (
            if not "%%V"=="old" (
                set "PY=%%P"
                set "PY_VER=%%V"
            )
        )
    )
)

if "%PY%"=="" (
    echo   [X] Python 3.11+ required. Download from https://python.org
    echo   Make sure "Add Python to PATH" is checked during install.
    pause
    exit /b 1
)
echo   [OK] Python %PY_VER% (%PY%)

:: 2. Virtual environment
if not exist ".venv" (
    %PY% -m venv .venv
    echo   [OK] Created virtual environment
) else (
    echo   [OK] Virtual environment exists
)

:: 3. Activate venv
call .venv\Scripts\activate.bat

:: 4. Upgrade pip
python -m pip install -q --upgrade pip 2>nul

:: 5. Install package with all dependencies
echo   [ ] Installing dependencies...
pip install -q -e "." 2>nul
if errorlevel 1 (
    echo   [!] pip install -e . failed, trying requirements.txt...
    pip install -q -r requirements.txt 2>nul
)
echo   [OK] Installed qwe-qwe + dependencies

:: 6. Verify critical dependencies
echo   [ ] Verifying dependencies...
set "MISSING="
python -c "import cryptography" 2>nul || set "MISSING=!MISSING! cryptography"
python -c "import openai" 2>nul || set "MISSING=!MISSING! openai"
python -c "from qdrant_client import QdrantClient" 2>nul || set "MISSING=!MISSING! qdrant-client"
python -c "from fastembed import TextEmbedding" 2>nul || set "MISSING=!MISSING! fastembed"
python -c "import rich" 2>nul || set "MISSING=!MISSING! rich"
python -c "import fastapi" 2>nul || set "MISSING=!MISSING! fastapi"
python -c "import uvicorn" 2>nul || set "MISSING=!MISSING! uvicorn"
python -c "import requests" 2>nul || set "MISSING=!MISSING! requests"
python -c "from PIL import Image" 2>nul || set "MISSING=!MISSING! Pillow"

if not "%MISSING%"=="" (
    echo   [!] Missing packages:%MISSING%
    echo   [ ] Installing missing packages...
    for %%M in (%MISSING%) do pip install -q %%M 2>nul
    echo   [OK] Installed missing packages
) else (
    echo   [OK] All dependencies verified
)

:: 7. Pre-download embedding model (first run takes ~2min otherwise)
echo   [ ] Pre-loading embedding model (one-time, ~200MB)...
python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')" 2>nul
if errorlevel 1 (
    echo   [!] Embedding model download failed — will retry on first use
) else (
    echo   [OK] Embedding model ready
)

:: 8. Create data directories
if not exist "logs" mkdir logs
if not exist "memory" mkdir memory
if not exist "skills" mkdir skills
if not exist "uploads" mkdir uploads
echo   [OK] Data directories ready

:: 9. Search for LLM servers
echo.
echo   Searching for LLM servers...
set "LM_FOUND=0"
for %%P in (1234 11434 8080) do (
    python -c "import requests; r=requests.get('http://localhost:%%P/v1/models',timeout=2); print(r.status_code)" 2>nul | findstr "200" >nul 2>&1 && (
        echo   [OK] LLM server found at localhost:%%P
        set "LM_FOUND=1"
    )
)
if "%LM_FOUND%"=="0" (
    echo   [!] No LLM server found on localhost
    echo       Start LM Studio or Ollama, load a model, then run qwe-qwe
)

:: 10. Summary
echo.
echo   ───────────────────────────
echo   Ready!
echo.
echo   Usage:
echo     .venv\Scripts\activate
echo.
echo     qwe-qwe              # terminal chat
echo     qwe-qwe --web        # web UI (http://localhost:7860)
echo.
echo     python cli.py         # alternative: run directly
echo     python server.py      # alternative: web server directly
echo.
pause
