"""密钥扫描（审查快照（2026-10-04）#ENGI-15 ②）。

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
import contextlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
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

#: 按平台的 release 资产：`(文件名, 归档格式)`。名字**照 GitHub 上真有的抄**（10-09 用 API
#: 现读出来的：Windows 那份是 `.zip`、后缀 `x64`；Linux/Darwin 才是 `.tar.gz`）。我第一版
#: 按"以为的形状"写成 `windows_x86_64.tar.gz` ⇒ 404 —— 而 404 在 fail-closed 下与"离线"长得
#: 一模一样，这种错只能靠**真装一次**照出来，读代码读不出来。
_VER = GITLEAKS_VERSION[1:]  # v8.18.4 → 8.18.4（资产名里的版本号不带 v）
_ASSET_BY_OS = {
    "Windows": (f"gitleaks_{_VER}_windows_x64.zip", "zip"),
    "Linux": (f"gitleaks_{_VER}_linux_x64.tar.gz", "tar"),
    "Darwin": (f"gitleaks_{_VER}_darwin_x64.tar.gz", "tar"),
}


def _tool_path() -> Path | None:
    on_path = shutil.which("gitleaks")
    if on_path:
        return Path(on_path)
    return None


def _extract_binary(archive: Path, fmt: str, binary_name: str) -> bytes | None:
    """从归档里取那一个二进制的内容；取不到返回 None（调用方如实失败）。

    只**读**那一个成员进内存，从不 `extractall` —— 所以归档里写什么逃逸路径都到不了盘上，
    落点永远是调用方给的 `cache/binary_name`。这比"解出来再防逃逸"少一层可以出错的机制。
    """
    if fmt == "zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if Path(info.filename).name == binary_name and not info.is_dir():
                    return zf.read(info)
        return None
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            if Path(member.name).name == binary_name and member.isfile():
                src = tf.extractfile(member)
                if src is None:
                    # 名义上是文件却取不出内容：如实失败，不猜成"空的也能用"。
                    return None
                return src.read()
    return None


def _download_asset(cache: Path, binary_name: str) -> Path | None:
    """按平台直连官方 release 资产；离线/失败/解不出都返回 None（交给 fail-closed）。

    Windows 上这条路从前**根本不存在**：`_install_to` 的注释写着"Windows 走 git-bash 或跳过"，
    而代码是 `installer = which(bash) if system != Windows else None` —— Windows 直接被设成
    None，git-bash 从来没被尝试过。后果不是崩，是**这台机器上 `密钥扫描` 恒退 2**：本地全量
    档的红线从此永远踩不动（"扫不成"的红与"从没扫过"在退出码上长得一模一样，正是本仓最忌讳
    的那种像）。`_GITLEAKS_URLS` 那两条地址从前写了没人用 —— 死代码也是第二份事实面，删了。
    """
    import platform
    import urllib.request

    picked = _ASSET_BY_OS.get(platform.system())
    if picked is None:
        print(f"[scan_secrets] 这个平台没有现成的 gitleaks 资产：{platform.system()}",
              file=sys.stderr)
        return None
    asset, fmt = picked
    url = f"https://github.com/gitleaks/gitleaks/releases/download/{GITLEAKS_VERSION}/{asset}"
    print(f"[scan_secrets] 直连拉取 {asset}（离线会失败，按红线当红）", file=sys.stderr)
    tmp = cache / asset
    target = cache / binary_name
    data: bytes | None = None
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        data = _extract_binary(tmp, fmt, binary_name)
    except Exception as exc:  # noqa: BLE001 — 装不到就是装不到，如实交给 fail-closed
        print(f"[scan_secrets] 直连拉取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    finally:
        tmp.unlink(missing_ok=True)
    if data is None:
        print(f"[scan_secrets] 资产里没有 {binary_name}：{asset}", file=sys.stderr)
        return None
    target.write_bytes(data)
    with contextlib.suppress(OSError):  # Windows 上执行位是个装饰，不该为此丢这次扫描
        target.chmod(0o755)
    return target


def _install_to(cache: Path) -> Path | None:
    """联网时拉一个临时副本到缓存目录；离线或失败都返回 None（交给 fail-closed）。

    两条路：非 Windows 先试官方 install 脚本（按平台解析资产，省事）；任何一条路最后都
    还有 `_download_asset` 的直连兜底。**Windows 只有直连这一条**（没有可靠的 bash）。
    CI 的 Linux runner 上这两条都通，红线可达。
    """
    import platform

    cache.mkdir(parents=True, exist_ok=True)
    binary = "gitleaks.exe" if platform.system() == "Windows" else "gitleaks"
    target = cache / binary
    if target.exists():
        return target
    if platform.system() != "Windows":
        installer = shutil.which("bash")
        if installer:
            print(
                "[scan_secrets] 本地无 gitleaks，用官方 install 脚本拉取（离线会失败，按红线当红）",
                file=sys.stderr,
            )
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
    return _download_asset(cache, binary)


def _tracked_files(repo: Path) -> list[str]:
    """"这份要发出去的源码"= tracked + 没被忽略的未跟踪。由 git 自己答，不抄第二份规则。

    从前这里靠 `--no-git` 直扫工作树 + 配置里一份目录黑名单去逼近同一个语义 —— 两处都是假的：
    `--no-git` 是**盘上全扫、根本不看 .gitignore**，而黑名单两种写法实测都 over-match
    （`[/\\]build[/\\]` 与裸 `build/` 一样都命中 `shell/build/icon.ico` —— 那是 tracked 的
    出货图标，实测过匹配）。影子树的范围由名单**正向**圈定，仓内 `.venv` 那 23 条第三方
    假阳连进范围的机会都没有；判据也不再需要任何目录形状。
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files -> {proc.returncode}：{proc.stderr.strip()[:200]}")
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _shadow_tree(repo: Path, files: list[str]) -> Path:
    """按名单拷一份影子树进临时目录（保留相对结构）；扫描跑在这份上。

    **拷不动就抛**，不许静默跳过：名单里有一个文件没进影子树，就等于那份源码**免检** ——
    这条红线最坏的形状正是"扫过了、绿了、其实少扫了一个文件"。`is_file()` 为假的那几种
    （竞态下刚被删、目录项）不在此列：它们本来就没有内容可扫，数出来报给调用方看。
    """
    root = Path(tempfile.mkdtemp(prefix="gitleaks-scope-"))
    missing: list[str] = []
    try:
        for rel in files:
            src = repo / rel
            if not src.is_file():
                missing.append(rel)
                continue
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # 抛出去：按"扫不成"处理，而不是按"没这个文件"处理
    except Exception:
        # 影子树含的是**要发布的源码副本**，建到一半失败也不能把它留在 %TEMP% 里等回收。
        shutil.rmtree(root, ignore_errors=True)
        raise
    if missing:
        print(
            f"[scan_secrets] 名单里有 {len(missing)} 项此刻不在盘上（竞态/目录项），"
            f"举例：{missing[:3]}",
            file=sys.stderr,
        )
    return root


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

    # 范围：**git 的名单**（tracked + 未忽略的未跟踪）搬进影子树。`--history` 那一档例外 ——
    # 它扫提交历史，范围本来就是 git 给的（`.venv` 从没被提交过，所以那边也扫不到）。
    # 名单拿不到（没装 git / 不是工作树 / 空仓库）一律按"扫不成"退 2：范围定不下来不等于干净。
    shadow: Path | None = None
    source = repo
    if not args.history:
        try:
            files = _tracked_files(repo)
        except Exception as exc:  # noqa: BLE001 — 任何拿不到名单的原因都是同一个后果
            print(
                f"[scan_secrets] ❌ 定不出扫描范围：{exc}（扫不成 ≠ 干净，退 2）",
                file=sys.stderr,
            )
            return 2
        if not files:
            print("[scan_secrets] ❌ git 名单是空的 —— 不像一份仓库，按扫不成退 2", file=sys.stderr)
            return 2
        shadow = _shadow_tree(repo, files)
        source = shadow
        print(f"[scan_secrets] 范围 = git 名单 {len(files)} 个文件（影子树）", file=sys.stderr)

    cmd = [str(tool), "detect", "--source", str(source), "--config", str(config),
           "--no-banner", "--exit-code", "1", "--redact"]
    if shadow is not None:
        # 影子树里没有 .git，也不该回头去读原仓库的历史 —— `--no-git` 在这里终于名副其实：
        # 扫的就是"本次要发出去的那一份源码"。
        cmd.append("--no-git")
    label = "全量历史" if args.history else "本次要发的源码"
    print(f"[scan_secrets] {label}扫描：{' '.join(cmd)}", file=sys.stderr)
    # 300s 硬超时（2026-10-09）：与依赖审计同一条纪律 —— 挂死必须变成一次**干净的红**，
    # 而不是把整条 CI job 吊到顶穿。顶穿的 run GitHub 不传日志，现场直接消失
    # （run 37804463056 就是这么失去证据的）。`--history` 那档本来就标了"较慢"，
    # 它由调用方自己决定跑不跑；CI 红线路径（本次要发的源码）5 分钟绰绰有余。
    #
    # `finally` 那一段是影子树带来的新义务，不是装饰：扫完/超时/任何异常都得把那份
    # 临时树收掉 —— 里面是**要发布的源码本身**，留在 %TEMP% 里等系统回收，等于让这条
    # 安全红线自己往外泄一份副本。`ignore_errors` 沿用本仓那两次实测（Windows 上临时
    # 目录里的句柄删不动、但内容已经不再被读）：收摊失败不该把一次扫完的结论弄没。
    #
    # 两条 except 各是一格 fail-closed：`TimeoutExpired` = 吊死；`OSError` = 工具压根起不来
    # （缓存里那个文件被截断/权限不对）。从前只有前者，后者会让本脚本**抛 traceback** ——
    # 门禁把 traceback 读成"这一步红了"是对的，但退码就不是约定的 2 了，而这条线的退出码
    # 是判据（0 干净 / 1 发现 / 2 扫不成）， traceback 那条形状等于新造第四种。
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            print(
                "[scan_secrets] ❌ gitleaks 300s 没扫完，按扫不成当红（exit=2）——"
                "挂死不该把整条 job 拖到被杀",
                file=sys.stderr,
            )
            return 2
        except OSError as exc:
            print(f"[scan_secrets] ❌ gitleaks 起不来：{exc}（扫不成 ≠ 干净，退 2）",
                  file=sys.stderr)
            return 2
    finally:
        if shadow is not None:
            shutil.rmtree(shadow, ignore_errors=True)
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
