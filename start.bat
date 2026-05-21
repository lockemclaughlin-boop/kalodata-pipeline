@echo off
REM Windows launcher — double-click to start the app.

cd /d "%~dp0"

if not exist ".venv\" (
    echo First-time setup...
    python -m venv .venv
    .venv\Scripts\pip install --upgrade pip
    .venv\Scripts\pip install -r requirements.txt
    .venv\Scripts\python -m playwright install chromium
)

.venv\Scripts\streamlit run src\app.py --server.port 8765
