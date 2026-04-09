@echo off
setlocal

cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo Missing virtual environment at .venv
  exit /b 1
)

if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"

set "PLAYWRIGHT_SKIP_BROWSER_INSTALL=1"
set "DATA_DIR=%CD%\data"

".venv\Scripts\python.exe" -m uvicorn server:app --host 0.0.0.0 --port 8000 1> "logs\panwatch.out.log" 2> "logs\panwatch.err.log"
