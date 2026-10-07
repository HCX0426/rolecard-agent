@echo off
rem =========================================================================
rem  rolecard-agent - one-time installer (fresh clone / clean machine)
rem  Usage: double-click, or run from cmd:  install.bat
rem  (kept ASCII-only on purpose: CJK text in bat files garbles on GBK consoles)
rem =========================================================================
setlocal
cd /d "%~dp0"

echo [1/5] Python venv (.venv)
if exist ".venv\Scripts\python.exe" goto :venv_ready
call :find_python "%~1" || goto :fail
"%PY%" -m venv .venv || goto :fail
:venv_ready

echo [2/5] backend deps: runtime lock (core + api + rag + cloud + mcp)
rem **Locked install** (2026-10-07): requirements-runtime.lock is pip-compiled from the
rem five runtime requirement mirrors; its coverage is pinned by the `lockfile parity`
rem ruler in check_consistency.py. The "why all five families" history lives in the
rem Dockerfile comments (R28-11/R28-12/R28-53) - a lock cures version drift, not a
rem missing family, and that remains the rulers' job.
".venv\Scripts\python.exe" -m pip install -r requirements-runtime.lock || goto :fail

echo [3/5] frontend: npm ci + build  (dist/ is what the server hosts)
pushd frontend
call npm ci || goto :fail
call npm run build || goto :fail
popd

echo [4/5] database schema + built-in roles (idempotent)
".venv\Scripts\python.exe" "scripts\init_db.py" || goto :fail

echo [5/5] done.
echo       Next: run start.bat  -  console opens at http://127.0.0.1:8000/
echo       Optional: requirements-ocr.txt needs its OWN venv (see that file first)
echo       Local gate (scripts/gate.py) additionally wants requirements-dev.txt -
echo       deliberately NOT part of a product install: dev deps (incl. PyInstaller)
echo       stay out of the runtime tree.
pause
exit /b 0

rem =========================================================================
rem Pick an interpreter that actually RUNS. Bare `python` / `python3` on PATH is
rem frequently the Microsoft Store *alias* - a stub that exits 49 (and opens the
rem Store page) without ever running Python - so a plain `python -m venv` can
rem fail with no usable message. `py` is tried first because the real launcher is
rem never shadowed by that alias. Override with either of:
rem     install.bat "D:\path\to\python.exe"
rem     set ROLECARD_PY=D:\path\to\python.exe  &&  install.bat
rem Order: argument -> ROLECARD_PY -> py/python/python3 on PATH -> the two
rem standard per-machine install dirs -> a failure message that names the alias.
rem =========================================================================
:find_python
set "PY=%~1"
if not defined PY set "PY=%ROLECARD_PY%"
if defined PY (
  "%PY%" -c "print(1)" >nul 2>nul && exit /b 0
  echo [install] not a working interpreter: %PY%
  exit /b 1
)
for %%C in (py python python3) do (
  "%%C" -c "print(1)" >nul 2>nul && if not defined PY set "PY=%%C"
)
if defined PY exit /b 0
for %%P in ("%LocalAppData%\Programs\Python\Python313\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python313\python.exe") do (
  if not defined PY if exist %%P set "PY=%%~P"
)
if defined PY exit /b 0
echo [install] No usable Python 3.13+ found on this machine.
echo [install]   If typing "python" opens the Microsoft Store: Settings - Apps -
echo [install]   Advanced settings - App execution aliases, turn OFF python.exe and
echo [install]   python3.exe, install Python ticking "Add python.exe to PATH", re-run.
echo [install]   Or point this installer at an interpreter you already have:
echo [install]     install.bat "D:\path\to\python.exe"
exit /b 1

:fail
echo.
echo [install] FAILED - see the error above.
pause
exit /b 1
