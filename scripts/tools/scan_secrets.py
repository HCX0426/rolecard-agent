"""密钥扫描（ENGI-15 ②）。

设计要点：
  * **fail-closed**：gitleaks 二进制不在、或装不上、或扫出来但放行判据没认下 —— 一律**红**
    （退出码非 0，并点名原因）。「扫不成」不等于「干净」，这与 ① 依赖审计同一条铁律。
  * **离线优先**：默认只跑 **committed-tree 扫描**（`--no-git` 对当前工作树源码），不碰网络；
    历史全量扫描需要 clone 历史，留给单独 job / 手动。CI 的红线只保证「本次要发的源码里没有
    裸密钥」，历史那一刀由各自负责的机器补。
  * **允许清单走 `.gitleaks.toml`**：真正的误报（测试 fixture 里的假 key、文档里的示例
    endpoint、截图内嵌的不可能字符串）用 gitleaks 自己的 `allowlist` 表达，集中在一处，
    改的时候能对照。本脚本**不**自己维护第二份白名单 —— 避免「脚本一份、toml 一份」漂开。
  * **没有 gitleaks 就装一个临时副本**（pip 同款做法在 ① 用过）：从官方 release 拉一个
    pinned 版本到缓存目录，且只在**联网**时尝试；离线环境装不了就按 fail-closed 退出。
  * 退出码约定（与门禁其它步骤对齐）：0 = 通过；2 = 工具不可用/扫不成（红线当红）；
    1 = 发现疑似密钥。

用法：
    python scripts/tools/scan_secrets.py [--repo .] [--json 报告.json]
不含 --history 时只扫当前工作树（CI 红线路径、离线）。加 --history 扫全量 git 历史
（需要 .git，且可能较慢）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# pinned gitleaks release（与 ① 的依赖审计同样的「锁版本」思路：不追 latest，
# 避免某天新版默认规则变严把整条 CI 打红又没人盯着）。
GITLEAKS_VERSION = "v8.18.4"

# 与 gate.py 同一条纪律：Windows 控制台默认 codepage 是 GBK，本仓输出里大量中文/符号，
# 不重配编码会在第一个 print 就抛 UnicodeEncodeError 退场（check_consistency 的
# `console encoding` 尺子就是盯这个）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 从官方源拉二进制的回退地址（主 + 镜像）。都是静态 release 资产，与 py 包无关。
_GITLEAKS_URLS = [
    f"https://github.com/gitleaks/gitleaks/releases/download/{GITLEAKS_VERSION}"
    "/gitleaks_{GITLEAKS_VERSION[1:]}_windows_x86_64.tar.gz",
    f"https://github.com/gitleaks/gitleaks/releases/download/{GITLEAKS_VERSION}"
    "/gitleaks_{GITLEAKS_VERSION[1:]}_linux_x86_64.tar.gz",
]


def _tool_path() -> Path | None:
    on_path = shutil.which("gitleaks")
    if on_path:
        return Path(on_path)
    return None


def _install_to(cache: Path) -> Path | None:
    """联网时拉一个临时副本到缓存目录；离线或失败都返回 None（交给 fail-closed）。

    用 gitleaks 官方 install 脚本（按平台解析正确的 release 资产，避免手拼 URL 404）。
    CI 的 Linux runner 上这一段能真正装到（有网络），于是红线可达；本机离线时
    install 脚本也装不到，按 fail-closed 退 2。
    """
    import platform
    import shutil

    cache.mkdir(parents=True, exist_ok=True)
    target = cache / ("gitleaks.exe" if platform.system() == "Windows" else "gitleaks")
    if target.exists():
        return target
    # 先试官方 install 脚本（Linux/macOS 走 bash；Windows 走 git-bash 或跳过）。
    installer = shutil.which("bash") if platform.system() != "Windows" else None
    if installer:
        msg = "[scan_secrets] 本地无 gitleaks，用官方 install 脚本拉取（离线会失败，按红线当红）"
        print(msg, file=sys.stderr)
        try:
            script = "https://raw.githubusercontent.com/gitleaks/gitleaks/master/scripts/install.sh"
            cmd_line = f"curl -sSfL {script} | bash -s -- -b {cache} {GITLEAKS_VERSION}"
            proc = subprocess.run(
                [installer, "-c", cmd_line],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0 and target.exists():
                return target
            tail = proc.stderr.strip()[-500:]
            print(f"[scan_secrets] install 脚本失败：{tail}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[scan_secrets] install 脚本异常：{exc}", file=sys.stderr)
    msg = "[scan_secrets] 装不到 gitleaks（离线或网络不可达），按红线当红（exit=2）"
    print(msg, file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="密钥扫描（gitleaks 包装，fail-closed）")
    parser.add_argument("--repo", default=".", help="仓库根（默认当前目录）")
    parser.add_argument("--json", default=None, help="把发现写成 JSON 报告")
    parser.add_argument("--history", action="store_true",
                        help="全量 git 历史扫描（需要 .git，较慢）")
    parser.add_argument("--cache-dir",
                        default=str(Path(tempfile.gettempdir()) / "gitleaks-cache"),
                        help="离线装二进制的缓存目录")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    config = repo / ".gitleaks.toml"
    if not config.exists():
        msg = "[scan_secrets] 缺少 .gitleaks.toml —— 放行判据无处可查，按红线当红"
        print(msg, file=sys.stderr)
        return 2

    tool = _tool_path() or _install_to(Path(args.cache_dir))
    if tool is None:
        msg = "[scan_secrets] 装不到 gitleaks（离线或网络不可达），按红线当红（exit=2）"
        print(msg, file=sys.stderr)
        return 2

    # 只扫 tracked 的源码树（排除 node_modules / 大二进制）。gitleaks 自带的 .gitignore
    # 尊重 + .gitleaks.toml 里再点一遍 allowlist/paths。
    cmd = [str(tool), "detect", "--source", str(repo), "--config", str(config),
           "--no-banner", "--exit-code", "1", "--redact"]
    if not args.history:
        # 当前工作树：不需要 git 历史，离线可跑。
        cmd.append("--no-git")
    scope = "全量历史" if args.history else "当前工作树"
    print(f"[scan_secrets] {scope}扫描：{' '.join(cmd)}", file=sys.stderr)
    # 300s 硬超时（2026-10-09）：与依赖审计同一条纪律 —— 挂死必须变成一次**干净的红**，
    # 而不是把整条 CI job 吊到顶穿。顶穿的 run GitHub 不传日志，现场直接消失
    # （run 37804463056 就是这么失去证据的）。`--history` 那档本来就标了"较慢"，
    # 它由调用方自己决定跑不跑；CI 红线路径（工作树）5 分钟绰绰有余。
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print(
            "[scan_secrets] ❌ gitleaks 300s 没扫完，按扫不成当红（exit=2）——"
            "挂死不该把整条 job 拖到被杀",
            file=sys.stderr,
        )
        return 2
    out = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode == 0:
        print("[scan_secrets] 未发现疑似密钥 ✅", file=sys.stderr)
        if args.json:
            data = json.dumps({"findings": []}, ensure_ascii=False, indent=2)
            Path(args.json).write_text(data, encoding="utf-8")
        return 0
    if proc.returncode == 1:
        # 真有发现：把 gitleaks 的摘要（已 redact）打到 stderr，写 JSON 报告。
        print("[scan_secrets] ❌ 发现疑似密钥（已 redact）：", file=sys.stderr)
        print(out, file=sys.stderr)
        if args.json:
            # gitleaks --report-format json 更结构化，但这里 redact 后只留摘要即可。
            data = json.dumps({"findings_summary": out[-2000:]}, ensure_ascii=False, indent=2)
            Path(args.json).write_text(data, encoding="utf-8")
        return 1
    # 其它退出码（config 错、工具崩、权限）：fail-closed。
    msg = f"[scan_secrets] gitleaks 异常退出（code={proc.returncode}），按红线当红："
    print(msg, file=sys.stderr)
    print(out, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
