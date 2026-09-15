@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Mini Agent - one-command local deploy
echo  Steps: install deps (first run only) - prepare config -
echo         build web UI - migrate database - serve on port 8000
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [FAIL] Python not found in PATH. Install Python 3.11+ first.
  goto end
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/6] Creating virtual environment .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [FAIL] python -m venv failed.
    goto end
  )
) else (
  echo [1/6] Virtual environment already present - skipped.
)

echo [2/6] Installing Python dependencies from requirements.lock ...
".venv\Scripts\python.exe" -m pip install -q -r requirements.lock
if errorlevel 1 (
  echo [FAIL] pip install failed.
  goto end
)

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo [3/6] Created .env from .env.example
  echo.
  echo        ACTION NEEDED: open .env and fill in DEEPSEEK_API_KEY,
  echo        then run start.cmd again.
  goto end
)
if not exist "models.toml" (
  copy /y "models.example.toml" "models.toml" >nul
  echo [3/6] Created models.toml from models.example.toml
  echo        Check endpoint and model name before using a real provider.
) else (
  echo [3/6] Config files present: .env , models.toml
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [FAIL] npm not found in PATH. Install Node.js 20+ first.
  goto end
)

if not exist "frontend\node_modules" (
  echo [4/6] Installing frontend dependencies ^(first run, may take a minute^) ...
  pushd frontend
  call npm install
  if errorlevel 1 (
    popd
    echo [FAIL] npm install failed.
    goto end
  )
  popd
) else (
  echo [4/6] Frontend dependencies already present - skipped.
)

echo [5/6] Building the web UI ...
pushd frontend
call npm run build
if errorlevel 1 (
  popd
  echo [FAIL] npm run build failed.
  goto end
)
popd

echo [6/6] Applying database migration ...
".venv\Scripts\alembic.exe" -c backend/alembic.ini upgrade head
if errorlevel 1 (
  echo [FAIL] alembic migration failed.
  goto end
)

echo.
echo ============================================================
echo  Ready. Open http://127.0.0.1:8000 in your browser.
echo  Press Ctrl+C to stop the server.
echo ============================================================
echo.
".venv\Scripts\python.exe" -m uvicorn --app-dir backend mini_agent.api:app --host 127.0.0.1 --port 8000

:end
if /i not "%~1"=="nopause" pause
