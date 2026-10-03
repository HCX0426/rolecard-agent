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
# 版本号从 shell/package.json 现读，不写死（09-28 轮 `R28-26`）：这里原先有两条硬编码的
# `0.1.0` —— 装上一步的路径，和下面那条"安装包进程退干净没有"的匹配。升版本时它们
# **静默失效而不是报错**：路径那条会在下一个语句炸，而进程匹配那条只会一直匹配不到任何东西，
# 于是"installer 没退出"这个检查永远通过（一个从不失败的检查比没有检查更坏）。
$shellPkg = Get-Content -Raw -Encoding UTF8 (Join-Path $root "shell\package.json") | ConvertFrom-Json
$shellVersion = $shellPkg.version
$installerName = "rolecard-agent-$shellVersion-x64.exe"
$installer = Join-Path $root "shell\release\$installerName"
$installedExe = Join-Path $env:LOCALAPPDATA "Programs\rolecard-agent\rolecard-agent.exe"
# 这条预检**只属于 -SkipBuild**：不建的时候就装现成的那份，而"现成的那份"按名字找 ——
# 版本号一变（10-01 `R28-62` 把两条线并成一条，0.1.0 ⇒ 0.3.0）它就该出声，而不是抱着
# `release/` 里那份旧命名的包一路装下去。反过来，**从零打一发时这份产物本来就还不存在**，
# 在这里拦等于把唯一一条能重打包的路堵死（实测：10-01 第十七包就是被这句拦在 [1/6] 之前的，
# 而那句报错写的"应该叫 0.3.0、现在只有 0.1.0"当时是完全正确的诊断 —— 正确的诊断用错了时机）。
if ($SkipBuild -and -not (Test-Path $installer)) {
    $found = @(Get-ChildItem (Join-Path $root "shell\release") -Filter "rolecard-agent-*.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    throw "installer not found: $installerName (shell/package.json says version=$shellVersion; present in shell\release: $($found -join ', '))"
}
# 打印一律用 ASCII：Windows PowerShell 在 GBK 控制台下打中文会花屏（`R26-24` 那一族）。

function EntryAsset([string]$file) {
    # 用 [regex]::Match 而不是 Select-String：后者返回的是 MatchInfo **数组**，
    # 在它上面点 `.Matches[0]` 会走 PowerShell 的成员枚举，取到的不一定是那个 Match 对象
    # （实测一边取到哈希、另一边取到空串，把一次本来正确的安装判成"包不是新的"）。
    $text = Get-Content -Raw -Encoding UTF8 -LiteralPath $file
    return [regex]::Match($text, "assets/(index-[A-Za-z0-9_-]+\.js)").Groups[1].Value
}

function ListenerOwnerPids([int]$port) {
    # 谁在听这个端口。Get-NetTCPConnection 是 Windows 自带的，但它在有些装法里缺模块，
    # 所以留一条 netstat 的退路 —— 两条都拿不到就报"问不出"，不假装"没人听"。
    try {
        $via = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
            ForEach-Object { $_.OwningProcess })
        if ($via.Count -gt 0) { return @($via | Select-Object -Unique) }
    } catch { }
    $rows = & netstat -ano | Select-String "[:.]$port\s+\S+\s+LISTENING"
    $pids = @($rows | ForEach-Object { ($_ -split '\s+')[-1] } | Where-Object { $_ -match '^\d+$' })
    return @($pids | Select-Object -Unique)
}


if (-not $SkipBuild) {
    Write-Host "[1/6] building (frontend -> sidecar -> nsis)"
    Push-Location (Join-Path $root "frontend")
    # 不再 `| Out-Null`：下面 electron-builder 那一步 09-28 就是因为吞了输出而报出"装进去的不是
    # 新构建"这种离真因十万八千里的话（`R28-35` 那一族）。退出码照样查，两件事不冲突 ——
    # 打出来的是"失败时能看见为什么"，不是"成功时多几十行"。
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "frontend build failed (exit=$LASTEXITCODE)" }
    Pop-Location
    & (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "scripts\build_sidecar.py")
    if ($LASTEXITCODE -ne 0) { throw "sidecar build failed (exit=$LASTEXITCODE)" }
    # 随包本地 OCR：另一个自包含产物，**必须用 .venv-ocr 的 python 打**（主环境没有 rapidocr/cv2，
    # 而那份 venv 的 python.exe 依赖构建机的 base 解释器 —— 所以拷 venv 不算打包，见 spec 头部）。
    # 脚本自己会冒烟一次（造一张图认字），不过就**不产出**，这里也就不会往下装。
    $ocrPy = Join-Path $root ".venv-ocr\Scripts\python.exe"
    if (-not (Test-Path $ocrPy)) {
        throw "no .venv-ocr at $ocrPy —— 装机版就没本地 OCR：先 python -m venv .venv-ocr 再 pip install -r requirements-ocr.txt -r requirements-package-ocr.txt"
    }
    & $ocrPy (Join-Path $root "scripts\build_ocr_worker.py")
    if ($LASTEXITCODE -ne 0) { throw "ocr-worker build/smoke failed (exit=$LASTEXITCODE)" }
    Push-Location (Join-Path $root "shell")
    # 这一步的输出**不再 Out-Null**：2026-09-28 实测它静默失败过（electron-builder 要清
    # `release/win-unpacked` 时 `app.asar` 被别的进程占着 -> EBUSY），而退出码没被检查，于是
    # 脚本抱着 `release/` 里上一次的旧安装包一路装到验货 A 才红，报的是"装进去的不是新构建"
    # —— 症状离病因差了十万八千里。这里宁可多打几十行，也不让打包失败再隐身。
    npm run package
    if ($LASTEXITCODE -ne 0) { throw "electron-builder failed (exit=$LASTEXITCODE)" }
    Pop-Location
} else {
    Write-Host "[1/6] build skipped (-SkipBuild)"
}

# 装之前先给**装着的那份**留一份能回滚的东西。这一步以前是人记着的，
# 2026-09-26 第六次打包就漏了（装完才补，那时已经回不去了）—— 所以它进脚本而不是进备忘录。
# sqlite 走 backup() 而不是复制：真库开着 WAL，直拷会得到主库与 -wal 不同步的半成品。
Write-Host "[2/6] backing up the installed data root"
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py (Join-Path $root "scripts\backup_data_root.py") --dest (Join-Path $root "build")
if ($LASTEXITCODE -ne 0) { throw "backup failed (exit=$LASTEXITCODE) —— 没备份就别装" }

# 打完之后这一问才是**两边都要**的：[1/6] 的 electron-builder 若按另一个名字出产物
# （`artifactName` 与 shell/package.json 的 version 漂开），这里必须把它实际产出了什么列出来，
# 而不是只报"找不到路径" —— 由 `core/artifacts.py` 那侧的门禁断言问同形，人看的这一侧问事实。
if (-not (Test-Path $installer)) {
    $built = @(Get-ChildItem (Join-Path $root "shell\release") -Filter "rolecard-agent-*.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    throw "build did not produce $installerName (version=$shellVersion; release/ now holds: $($built -join ', '))"
}

Write-Host "[3/6] closing the installed app (if running)"
$targets = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $installedExe })
$main = $targets | Where-Object { $_.Name -eq "rolecard-agent.exe" } | Select-Object -First 1
if ($null -ne $main) {
    # 只发 WM_CLOSE 是关不掉这个壳的（「关闭控制台=隐藏」是设计），所以直接 /T /F 收整棵树。
    # 输出丢掉可以（它只有 "SUCCESS: ..." 这种话），**退出码不能丢**（`R28-35`）：以前
    # `| Out-Null` 之后直接睡 3 秒继续装，于是"进程根本没被关掉"这一件事被推给后面
    # 一句 cryptic 的"文件被占用"。但退出码本身也不够 —— taskkill 对"找不到那个 PID"
    # 返回的也是失败，而那种情况其实是我们想要的结果。所以判据用**它还在不在**：
    # 轮询到没有为止，超时就把 pid 报出来。
    & taskkill /PID $main.ProcessId /T /F | Out-Null
    $gone = $false
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 500
        $still = @(Get-CimInstance Win32_Process -Filter "ProcessId=$($main.ProcessId)" -ErrorAction SilentlyContinue)
        if ($still.Count -eq 0) { $gone = $true; break }
    }
    if (-not $gone) {
        throw "could not close the running app (pid $($main.ProcessId) still alive after 5s) - the installer would fail on locked files; close it from the tray and re-run"
    }
    Write-Host "      closed pid=$($main.ProcessId) and its tree"
} else {
    Write-Host "      not running, nothing to close"
}

Write-Host "[4/6] silent install"
$t0 = Get-Date
$p = Start-Process -FilePath $installer -ArgumentList "/S" -Wait -PassThru
$secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
Write-Host "      exit=$($p.ExitCode) elapsed=${secs}s"
if ($p.ExitCode -ne 0) { throw "installer returned $($p.ExitCode)" }
$lingering = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like "rolecard-agent-$shellVersion*" })
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
    Write-Host "[5/6] launching"
    # 端口与壳同一条口径（`shell/main/backend.ts` 的 `envPort()`：`ROLECARD_API_PORT`，默认 8000）。
    # 这里从前**写死 8000**，而开发态那条服务也在 8000（`docs/开发流程.md` 的"换后端=换库"那节
    # 自陈过这个撞法）—— 于是 `health=200` 完全可能是**dev 那份后端**答的：验的从来不是刚装上的东西。
    $apiPort = 8000
    if ($env:ROLECARD_API_PORT -and $env:ROLECARD_API_PORT -match '^\d+$') {
        $apiPort = [int]$env:ROLECARD_API_PORT
    }
    $base = "http://127.0.0.1:$apiPort"
    Start-Process -FilePath $installedExe | Out-Null
    # 轮询而不是睡死一个没被量过的 20 秒。
    $answer = $null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        try {
            $h = Invoke-WebRequest -Uri "$base/api/health" -UseBasicParsing -TimeoutSec 5
            if ($h.StatusCode -eq 200) { $answer = $h.Content; break }
        } catch { }
    }
    if (-not $answer) {
        throw "app is up but /api/health did not answer on :$apiPort within 60s"
    }
    # **谁**在答，比"有人答"重要：按端口找监听者，再问它的可执行路径在不在安装目录下。
    $installedDir = Split-Path -Parent $installedExe
    $owners = @(ListenerOwnerPids $apiPort)
    if ($owners.Count -eq 0) {
        throw "can't tell who answers :$apiPort (no listener found via Get-NetTCPConnection or netstat) - refusing to print OK"
    }
    $paths = @($owners | ForEach-Object {
        (Get-CimInstance Win32_Process -Filter "ProcessId=$_" -ErrorAction SilentlyContinue).ExecutablePath
    }) | Where-Object { $_ }
    $mine = @($paths | Where-Object { $_ -like "$installedDir*" })
    if ($mine.Count -eq 0) {
        throw ("answers on :$apiPort come from a process NOT under the install dir: " +
            ($paths -join ' | ') +
            ' - 十有八九是开发态那条服务占着端口（docs/开发流程.md 那条「换后端=换库」）。' +
            '先把 dev 停掉，或给这次验收设 ROLECARD_API_PORT 换一个端口再来一遍。')
    }
    Write-Host "      health=200 answered by pid=$($owners -join ',') :: $($mine -join ', ')"
    # 顺带问一句"它自报是哪一版"（P0-1 那条构建指纹）：路径已经证明是装好的那个，
    # 指纹再证明它里面跑的就是刚打的那份后端字节。两问各挡一半：
    # 只问指纹会被"dev 也在同一个 HEAD 上"骗过去，只问路径会被"装的是上一包"骗过去。
    try {
        $reported = (($answer | ConvertFrom-Json).build).sha
    } catch { $reported = $null }
    $builtInfo = Join-Path $root "build\build_info.json"
    $builtSha = $null
    if (Test-Path $builtInfo) {
        $builtSha = ((Get-Content -Raw -Encoding UTF8 $builtInfo) | ConvertFrom-Json).git_sha
    }
    # 两侧都**至少 12 个字符**才比：build_info.json 里写的是 `unknown`（读不到 git 时），
    # 直接 Substring(0,12) 会抛一个"长度不合法"的错，把一次正常的安装报成一句看不懂的栈。
    if ($reported -and $builtSha -and $reported.Length -ge 12 -and "$builtSha".Length -ge 12) {
        $wantSha = "$builtSha".Substring(0, 12)
        Write-Host "      build sha: installed=$reported built=$wantSha"
        if ($reported -ne $wantSha) {
            throw "installed backend reports $reported while the build we just packaged is $wantSha"
        }
    } else {
        Write-Host ("      ^ 没比构建指纹（installed=$reported / built=$builtSha）" +
            " - 两侧任一是 unknown 或缺失就只打这一行，不拿它当判据，也不假装比过")
    }
    # 装机版到底有没有本地 OCR：问**刚装的那台后端**的服务页，而不是看文件在不在。
    # 为什么必须问后端：`resources/ocr-worker/ocr-worker.exe` 躺着但跑不起来（缺 dll、被杀软拦、
    # 路径拼错）与"随包带了本地 OCR"是同一句人话下的两种命运，只有运行时那一格知道答案。
    try {
        $svcJson = (Invoke-WebRequest -Uri "$base/api/services" -UseBasicParsing -TimeoutSec 30).Content
        $svc = $svcJson | ConvertFrom-Json
        $ocrRow = @($svc.services | Where-Object { $_.key -eq 'ocr' })[0]
        if (-not $ocrRow) { throw "服务页里没有 ocr 那一栏" }
        $rapid = @($ocrRow.candidates | Where-Object { $_.id -eq 'rapidocr' })[0]
        if (-not $rapid) { throw "OCR 栏里没有 rapidocr 候选" }
        if (-not $rapid.available) { throw "装机版本地 OCR 不可用：$($rapid.reason)" }
        Write-Host "      本地 OCR: $($rapid.reason)（生效值=$($ocrRow.effective)）"
    } catch {
        throw "随包 OCR 验收失败：$_"
    }
} else {
    Write-Host "[5/6] not launched (-NoLaunch)"
}
Write-Host "[6/6] purity of the installed data root (read-only)"
# 为什么挂在装完之后：`R28-49` 实测过"换数据根那一刻，旧根里的测试痕迹升级成生产数据" ——
# 真库 chroma 里躺着 8 条夹具向量（同一组假血糖数），而台账里连行都没有。装包 / 从备份恢复 /
# 换根这三种动作之后，新根上最该有的一发读数就是这个。它**刻意不 throw**：夹具是历史事实，
# 为一件不影响本次安装成败的事把已经装好的应用再关一次，只会逼人再去点一次「完成」。
$liveRoot = Join-Path $env:LOCALAPPDATA "rolecard-agent"
$py = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if ((Test-Path $py) -and (Test-Path $liveRoot)) {
    & $py (Join-Path $PSScriptRoot "check_root_purity.py") --root $liveRoot
    # 逐条查退出码：pwsh 里 `a; b` 只认最后一条，那是 R28-26 那族吞错的 Windows 版。
    if ($LASTEXITCODE -ne 0) { Write-Host "      ^ 纯度尺子报红（不挡安装，但要有人看一眼上面那几行）" }
} else {
    Write-Host "      跳过：这台机器上没有 .venv 或安装根（CI 上就是这种形状，不算失败）"
}

Write-Host "OK installed and verified"
