@echo off
rem =========================================================================
rem  rolecard-agent - daily launcher
rem  Starts Ollama (if installed and not running) and the console on ONE port:
rem  http://127.0.0.1:8000/  (FastAPI serves both /api/* and the built frontend)
rem =========================================================================
setlocal
cd /d "%~dp0"

rem 本机专属路径集中在一处：换机器只改这一行（其余地方引用 %OLLAMA_DIR%）。
set "OLLAMA_DIR=D:\code\Ollama"

if not exist ".venv\Scripts\python.exe" (
  echo [start] .venv not found. Run install.bat first.
  pause
  exit /b 1
)

rem ---- 1) Ollama: start if not reachable --------------------------------
curl -s --max-time 2 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
  if exist "%OLLAMA_DIR%\ollama.exe" (
    echo [start] launching Ollama from %OLLAMA_DIR% ...
    set "OLLAMA_MODELS=%OLLAMA_DIR%\models"
    start "" /min "%OLLAMA_DIR%\ollama.exe" serve
    rem give the model server a moment to bind the port
    timeout /t 3 /nobreak >nul
  ) else (
    echo [start] Ollama not found at %OLLAMA_DIR% - chat needs a model backend.
    echo         Set MODEL_BACKENDS to a reachable OpenAI-compatible endpoint,
    echo         or edit the OLLAMA_DIR line in this file to point at your ollama.exe.
  )
) else (
  echo [start] Ollama already running.
)

rem ---- 2) console + API on one port -------------------------------------
rem 注意：scripts/run_api.py **会读取仓库根的 .env**（真实环境变量优先），所以这里
rem 的 set 只是「无 .env 时的兜底」。MODEL_THINKING_MODELS = 开启思考过程的模型名单
rem （界面显示折叠式思考面板）；不设 = 不输出思考。改模型请同步改这一行。
set "MODEL_THINKING_MODELS=qwen3-vl:8b"
echo [start] console: http://127.0.0.1:8000/   (Ctrl+C in this window stops it)
start "" http://127.0.0.1:8000/
".venv\Scripts\python.exe" "scripts\run_api.py"
