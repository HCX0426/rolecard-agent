@echo off
rem =========================================================================
rem  rolecard-agent - daily launcher
rem  Starts Ollama (if installed and not running) and the console on ONE port:
rem  http://127.0.0.1:8000/  (FastAPI serves both /api/* and the built frontend)
rem =========================================================================
setlocal
cd /d "%~dp0"

rem Ollama 在哪，**不写死进仓库**（2026-10-04 审查快照那条"本机专属路径入库"）：
rem   1) 环境变量 OLLAMA_DIR —— 便携版/非默认安装指它（set OLLAMA_DIR=D:\path\Ollama）；
rem   2) 官方 Windows 安装器的两个默认落点（按用户 / 整机）；
rem   3) PATH（where）。
rem 顺序刻意与 shell/main/ollama.ts 的 candidates()（ROLECARD_OLLAMA_BIN 那族）一致：
rem 两处各写一份是语言隔离的代价，**顺序漂了就是第二个事实面** —— 改这三行先看那处。
if not defined OLLAMA_DIR if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_DIR=%LOCALAPPDATA%\Programs\Ollama"
if not defined OLLAMA_DIR if exist "%ProgramFiles%\Ollama\ollama.exe" set "OLLAMA_DIR=%ProgramFiles%\Ollama"
if not defined OLLAMA_DIR if exist "%ProgramFiles(x86)%\Ollama\ollama.exe" set "OLLAMA_DIR=%ProgramFiles(x86)%\Ollama"
if not defined OLLAMA_DIR for /f "delims=" %%i in ('where ollama.exe 2^>nul') do if not defined OLLAMA_DIR set "OLLAMA_DIR=%%~dpi"

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
    rem 便携布局（可执行文件旁边就有 models）才接管模型目录；官方安装的模型在
    rem %LOCALAPPDATA%\Ollama\models，乱指会把已装模型整个藏起来。env 里自己设过
    rem OLLAMA_MODELS 的话这里不碰（setlocal 只影响本窗口，但顺序上先到先得没意义，不覆盖）。
    if exist "%OLLAMA_DIR%\models" if not defined OLLAMA_MODELS set "OLLAMA_MODELS=%OLLAMA_DIR%\models"
    start "" /min "%OLLAMA_DIR%\ollama.exe" serve
    rem give the model server a moment to bind the port
    timeout /t 3 /nobreak >nul
  ) else (
    echo [start] Ollama not found - chat needs a model backend.
    echo         Set OLLAMA_DIR to your Ollama folder ^(the one containing ollama.exe^),
    echo         or install it to %%LOCALAPPDATA%%\Programs\Ollama, or set MODEL_BACKENDS
    echo         to a reachable OpenAI-compatible endpoint instead.
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
