# 一键开发环境（P7，2026-10-26）：前置自检 + 同时起后端 / 前端 dev / 桌宠壳。
#
# 存在的理由：三件套从前分开起（run_api.py 一个窗口、npm run dev 一个、shell 又一个），
# 每个新环境都要重背一遍"谁先起、连哪个端口、壳会不会重复起后端"——三张分开起的
# 步骤散在开发流程.md 的速查表里，缺一根把它们串起来的线。
#
# 用法（仓库根）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev.ps1
#     -NoShell     不开桌宠壳（只后端 + vite，纯 API/前端迭代够用；shell 缺依赖时它也会自己跳）
#     -NoFrontend  不开 vite（只用打包面 8000 上的 dist 时）
#
# 三条口径（都是实测过的既有行为，脚本只是把它们接成一条命令）：
# * **复用优先**：8000 已有人答 /api/health ⇒ 视为"后端已在跑"，不重开（壳的
#   Backend.ensure 同款语义：端口有人应就直接连）。5173 有人听同理。
# * **壳吃仓库 .venv**：shell 的 dev 形态默认 `ROLECARD_PYTHON=.venv\Scripts\python.exe`
#   与 `ROLECARD_API_SCRIPT=scripts\run_api.py`（shell/main/backend.ts），所以后端
#   **先起**——壳探到 8000 有人应就安静连上，不会再 spawn 第二个后端。
# * **自检不修数据**：发现 venv/锁/node_modules 缺失只报"缺什么、去哪补"，
#   不自作主张装（装依赖是 install.bat 的职责，npm i 是网络大动作）。
#   唯一自动修的是钩子指向（`git config core.hooksPath .githooks`）——仓内配置、
#   幂等、零网络，装环境清单里本来就有这一步，忘了它只是没背书而已。

[CmdletBinding()]
param(
    [switch]$NoShell,
    [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Say([string]$m) { Write-Host "[dev] $m" }
function Die([string]$m) { Write-Host "[dev] ❌ $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 前置自检
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { Die ".venv 不存在 —— 先跑 install.bat（装环境是一次性的，dev.ps1 不代劳）" }
try {
    & $Py -c "import fastapi" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw }
} catch { Die ".venv 在但 fastapi 不可 import —— 锁没装齐：.venv\Scripts\python.exe -m pip install -r requirements\requirements.lock" }
if (-not (Test-Path (Join-Path $Root "requirements\requirements.lock"))) { Die "requirements/requirements.lock 缺席（仓内容不对，先对账 git 状态）" }

# 钩子指向：幂等自动修（见头部第三条口径）
$hooks = (& git -C $Root config core.hooksPath 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $hooks) {
    & git -C $Root config core.hooksPath .githooks
    Say "core.hooksPath 没设过 —— 已代为指到 .githooks（L0 拦截即刻生效）"
}

$Port8000Busy = $null -ne (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
$health = $null
try {
    $health = (Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode
} catch { }
if ($Port8000Busy -and -not $health) {
    Die "8000 有人听但不答 /api/health —— 那不是本后端。先弄清是谁占的（Get-NetTCPConnection -LocalPort 8000 | Select-Object OwningProcess），别把真故障当'已在跑'"
}

# ---------------------------------------------------------------- 1) 后端
if ($health) {
    Say "后端已在跑（/api/health 答 $health），复用，不重开"
} else {
    Say "起后端 run_api.py ..."
    Start-Process -FilePath $Py -ArgumentList "scripts\run_api.py" -WorkingDirectory $Root `
        -WindowStyle Minimized | Out-Null
    $ok = $false
    foreach ($i in 1..40) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) { Die "后端 20s 没答 /api/health —— 看那个最小化的 run_api 窗口找原因" }
    Say "后端健康（等 $([int]($i*0.5))s）"
}

# ---------------------------------------------------------------- 2) 前端 vite
if (-not $NoFrontend) {
    $viteBusy = $null -ne (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue)
    if ($viteBusy) {
        Say "5173 已有人听（vite 大概已在跑），跳过启动"
    } else {
        if (-not (Test-Path (Join-Path $Root "frontend\node_modules"))) { Die "frontend/node_modules 缺席 —— 先 cd frontend; npm ci" }
        Say "起前端 vite（5173，/api 代理到 8000）..."
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory (Join-Path $Root "frontend") `
            -WindowStyle Minimized | Out-Null
        # 探针走 localhost 不走 127.0.0.1：vite 在 Windows 上实测只绑 IPv6 `::1`
        # （10-26 验收现场：127.0.0.1:5173 连不上而进程明明在听），localhost 两族都试。
        $ok = $false
        foreach ($i in 1..40) {
            Start-Sleep -Milliseconds 500
            try {
                $r = Invoke-WebRequest -Uri "http://localhost:5173/" -UseBasicParsing -TimeoutSec 2
                if ($r.StatusCode -eq 200) { $ok = $true; break }
            } catch { }
        }
        if (-not $ok) { Die "vite 20s 没答 5173 —— 看那个最小化的 npm 窗口" }
        Say "前端就绪"
    }
}

# ---------------------------------------------------------------- 3) 桌宠壳
if (-not $NoShell) {
    if (-not (Test-Path (Join-Path $Root "shell\node_modules\electron"))) {
        Say "⚠️ shell 的 electron 没装（或只装了 wrapper 没下二进制）—— 跳过壳。"
        Say "   要它：cd shell; npm install; npx electron/install.js（国内网先设 ELECTRON_MIRROR，见开发流程.md 壳那节）"
    } else {
        Say "起桌宠壳（npm run dev：tsc + electron，它会连上面这个 8000 后端）..."
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory (Join-Path $Root "shell") `
            -WindowStyle Minimized | Out-Null
    }
}

Say "──────── 三件套状态 ────────"
Say ("后端   http://127.0.0.1:8000/   （控制台 = dist 静态托管；健康：" + $(try { (Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode } catch { "已掉线？" }) + "）")
if (-not $NoFrontend) { Say "前端   http://127.0.0.1:5173/   （热更新，/api 代理到 8000）" }
if (-not $NoShell)    { Say "壳     桌宠窗 + 控制台窗（关控制台=收起；退出走托盘）" }
Say "收摊：关掉各自的最小化窗口即可；后端窗口关 = 8000 释放"
