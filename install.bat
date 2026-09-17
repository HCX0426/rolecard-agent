@echo off
rem =========================================================================
rem  rolecard-agent - one-time installer (fresh clone / clean machine)
rem  Usage: double-click, or run from cmd:  install.bat
rem  (kept ASCII-only on purpose: CJK text in bat files garbles on GBK consoles)
rem =========================================================================
setlocal
cd /d "%~dp0"

echo [1/5] Python venv (.venv)
if exist ".venv\Scripts\python.exe" (
  echo       already exists, skip
) else (
  python -m venv .venv || goto :fail
)

echo [2/5] backend deps: core + api + rag
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-api.txt -r requirements-rag.txt || goto :fail

echo [3/5] frontend: npm ci + build  (dist/ is what the server hosts)
pushd frontend
call npm ci || goto :fail
call npm run build || goto :fail
popd

echo [4/5] database schema + built-in roles (idempotent)
".venv\Scripts\python.exe" "scripts\init_db.py" || goto :fail

echo [5/5] done.
echo       Next: run start.bat  -  console opens at http://127.0.0.1:8000/
echo       Optional: pip install -r requirements-cloud.txt  for cloud backends
echo                 requirements-ocr.txt needs its OWN venv (see that file first)
pause
exit /b 0

:fail
echo.
echo [install] FAILED - see the error above.
pause
exit /b 1
