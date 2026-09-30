# =============================================================================
# ps_utf8.ps1 —— 在本环境（PowerShell 5.1 · -NoProfile · 中文 Windows）统一 UTF-8
#
# 用法（每次命令前导，dot-source 即可）：
#     . .\scripts\ps_utf8.ps1; <你的命令>
#   或    . <仓库根>\scripts\ps_utf8.ps1; <你的命令>
#
# 为什么要它：WorkBuddy 的 PowerShell 工具以 -NoProfile 启动，$PROFILE 不会被加载；
# 而本机三个默认值都会导致中文坑：
#   [Console]::OutputEncoding = gb2312  → 子进程的 UTF-8 stdout 被按 GBK 解码 = 乱码
#   $OutputEncoding           = us-ascii → 喂给原生命令的中文参数降级 = 变 "?"
#   Out-File 默认 Unicode                → `>` 重定向写 UTF-16 = 读起来像二进制
#
# 三条赋值各自独立 try：某些无控制台宿主下 Console 赋值会抛，
# 不能让它连带跳过后两条（曾因此误判为"设置无效"）。
# =============================================================================

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
try { [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
try { $OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
try { chcp 65001 > $null } catch { }

$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'
$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
