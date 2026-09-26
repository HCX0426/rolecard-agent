# 装当前构建出来的 NSIS 包：静默安装 + 验货 + 装完启动。
#
# 为什么要专门有这个脚本（09-26 实测，别再靠记忆）：安装包**是**认 `/S` 的 ——
# 用 PowerShell 的 `Start-Process -ArgumentList "/S" -Wait` 传参，42.3 秒装完、
# exit 0、不弹任何向导、不留任何进程。但在 git-bash 里直接 `./rolecard-agent-....exe /S`
# 会被 MSYS 把 `/S` 当成 POSIX 路径去转换 ⇒ 弹出安装向导并停在「完成」页，
# 而调用方那条命令会一直挂着等一个不会自己点的按钮。
#
# 用法（在仓库根）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_package.ps1
#     -SkipBuild   不重新构建，直接装 shell/release 里现成的那份
#     -NoLaunch    只装，装完不启动（用来验完再自己决定什么时候开）
#
# 只关"装在 %LOCALAPPDATA%\Programs\rolecard-agent 下的那一份"，按**完整可执行路径**
# 精确匹配 —— 不按镜像名乱杀进程是这项目的铁律（同一台机器上可能还跑着别的同名进程）。

param(
    [switch]$SkipBuild,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $root "shell\release\rolecard-agent-0.1.0-x64.exe"
$installedExe = Join-Path $env:LOCALAPPDATA "Programs\rolecard-agent\rolecard-agent.exe"
# 打印一律用 ASCII：Windows PowerShell 在 GBK 控制台下打中文会花屏（`R26-24` 那一族）。

function EntryAsset([string]$file) {
    # 用 [regex]::Match 而不是 Select-String：后者返回的是 MatchInfo **数组**，
    # 在它上面点 `.Matches[0]` 会走 PowerShell 的成员枚举，取到的不一定是那个 Match 对象
    # （实测一边取到哈希、另一边取到空串，把一次本来正确的安装判成"包不是新的"）。
    $text = Get-Content -Raw -LiteralPath $file
    return [regex]::Match($text, "assets/(index-[A-Za-z0-9_-]+\.js)").Groups[1].Value
}


if (-not $SkipBuild) {
    Write-Host "[1/4] building (frontend -> sidecar -> nsis)"
    Push-Location (Join-Path $root "frontend")
    npm run build | Out-Null
    Pop-Location
    & (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "scripts\build_sidecar.py")
    if ($LASTEXITCODE -ne 0) { throw "sidecar build failed (exit=$LASTEXITCODE)" }
    Push-Location (Join-Path $root "shell")
    npm run package | Out-Null
    Pop-Location
} else {
    Write-Host "[1/4] build skipped (-SkipBuild)"
}
if (-not (Test-Path $installer)) { throw "installer not found: $installer" }

Write-Host "[2/4] closing the installed app (if running)"
$targets = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $installedExe })
$main = $targets | Where-Object { $_.Name -eq "rolecard-agent.exe" } | Select-Object -First 1
if ($null -ne $main) {
    # 只发 WM_CLOSE 是关不掉这个壳的（「关闭控制台=隐藏」是设计），所以直接 /T /F 收整棵树。
    & taskkill /PID $main.ProcessId /T /F | Out-Null
    Start-Sleep -Seconds 3
    Write-Host "      closed pid=$($main.ProcessId) and its tree"
} else {
    Write-Host "      not running, nothing to close"
}

Write-Host "[3/4] silent install"
$t0 = Get-Date
$p = Start-Process -FilePath $installer -ArgumentList "/S" -Wait -PassThru
$secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
Write-Host "      exit=$($p.ExitCode) elapsed=${secs}s"
if ($p.ExitCode -ne 0) { throw "installer returned $($p.ExitCode)" }
$lingering = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like "rolecard-agent-0.1.0*" })
if ($lingering.Count -gt 0) { throw "installer did not exit: $($lingering.Count) process(es) left" }

# 验货 A：装进去的那份 dist 必须是仓库刚构建出来的那份。判据用 `index.html` 里引的
# 入口资源哈希 —— 它比"文件存在"强，因为旧包里也有同名文件，只有哈希会随构建变。
$want = EntryAsset (Join-Path $root "frontend\dist\index.html")
$havePath = Join-Path $env:LOCALAPPDATA "Programs\rolecard-agent\resources\rolecard-backend\_internal\frontend\dist\index.html"
if (-not (Test-Path $havePath)) { throw "installed bundle has no frontend/dist/index.html" }
$have = EntryAsset $havePath
Write-Host "      entry asset: repo=$want installed=$have"
if ($want -ne $have) { throw "installed bundle is NOT the fresh build (asset hash differs)" }

# 验货 B：后端也要有它自己的判据。上面那条只证明"界面是新的" —— 一次纯后端的改动
# （09-26 那次主动开口的闸门修法就是）重建出来的 dist 哈希**一字不差**，于是旧后端
# 蒙混过关也会打印出一行 OK。这里比对刚构建的 sidecar exe 与装进去那份的 sha256：
# NSIS 是逐字节复制，相等就等于"跑的就是刚从这份源码打出来的后端"。
$bUILT = Join-Path $root "build\sidecar\rolecard-backend\rolecard-backend.exe"
$installedBackend = Join-Path $env:LOCALAPPDATA `
    "Programs\rolecard-agent\resources\rolecard-backend\rolecard-backend.exe"
if (-not (Test-Path $bUILT)) { throw "sidecar build output not found: $bUILT" }
$a = (Get-FileHash -LiteralPath $bUILT -Algorithm SHA256).Hash
$b = (Get-FileHash -LiteralPath $installedBackend -Algorithm SHA256).Hash
Write-Host "      backend sha256: built=$($a.Substring(0,12)) installed=$($b.Substring(0,12))"
$why = "  —— 用 -SkipBuild 装了一份旧包时这条是预期会红的：装进去的不是 build/sidecar 那份"
if ($a -ne $b) { throw ("installed BACKEND is not the fresh build (sha256 differs)" + $why) }

if (-not $NoLaunch) {
    Write-Host "[4/4] launching"
    Start-Process -FilePath $installedExe | Out-Null
    Start-Sleep -Seconds 20
    try {
        $h = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 10
        Write-Host "      health=$($h.StatusCode)"
    } catch {
        throw "app is up but /api/health did not answer: $($_.Exception.Message)"
    }
} else {
    Write-Host "[4/4] not launched (-NoLaunch)"
}
Write-Host "OK installed and verified"
