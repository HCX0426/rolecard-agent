@echo off
rem =========================================================================
rem  rolecard-agent - daily launcher
rem  Starts Ollama (if installed and not running) and the console on ONE port:
rem  http://127.0.0.1:8000/  (FastAPI serves both /api/* and the built frontend)
rem =========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [start] .venv not found. Run install.bat first.
  pause
  exit /b 1
)

rem ---- 1) Ollama: start if not reachable --------------------------------
curl -s --max-time 2 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
  if exist "D:\code\Ollama\ollama.exe" (
    echo [start] launching Ollama from D:\code\Ollama ...
    set "OLLAMA_MODELS=D:\code\Ollama\models"
    start "" /min "D:\code\Ollama\ollama.exe" serve
    rem give the model server a moment to bind the port
    timeout /t 3 /nobreak >nul
  ) else (
    echo [start] Ollama not found at D:\code\Ollama - chat needs a model backend.
    echo         Set MODEL_BACKENDS to a reachable OpenAI-compatible endpoint,
    echo         or edit this file to point at your ollama.exe.
  )
) else (
  echo [start] Ollama already running.
)

rem ---- 2) console + API on one port -------------------------------------
rem 本项目**不自动读取 .env**（未引入 dotenv，配置一律走环境变量），因此需要在
rem 这里显式 export。MODEL_THINKING_MODELS = 开启思考过程的模型名单（界面显示
rem 折叠式思考面板）；不设 = 不输出思考。改模型请同步改这一行。
set "MODEL_THINKING_MODELS=qwen3-vl:8b"
echo [start] console: http://127.0.0.1:8000/   (Ctrl+C in this window stops it)
start "" http://127.0.0.1:8000/
".venv\Scripts\python.exe" "scripts\run_api.py"
