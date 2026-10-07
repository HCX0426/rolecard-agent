"""尺子的共享状态与遍历助手 —— 所有判据模块从这里拿"世界"。

刻意只有这几样：全局结算面（fails/warns/passed 与 out）、仓库文件遍历
（IGNORED_DIRS 剪枝 + 缓存）、注释剥离。多一个进来的候选都要先问：
"它是三处以上判据共用的世界观吗？"——不是就留在自己的判据模块里。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]  # 本文件住 scripts/consistency/，比旧位置深一层

fails: list[str] = []
warns: list[str] = []
passed = 0


# Directories that must never be walked. `ROOT.rglob("*.py")` happily descends into a
# virtualenv, which made the line-budget metric report 300k lines of site-packages instead
# of the project (C24).
IGNORED_DIRS = {
    ".git",
    ".venv",
    ".venv-dev",
    ".venv-ocr",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    "data",
    "node_modules",
    # `build/` 是本地脚手架（sidecar 解包、安装包、日志、临时克隆），`.gitignore` 整目录挡着。
    # 09-26 干净克隆彩排时我在 build/ci-clone 里放了一份仓库副本，两条检查立刻把**副本**里
    # 的脚本当成待检文件判红 —— 与 C24 那次"把 site-packages 数成项目代码"同一族：
    # 尺子必须只看这一个世界。
    "build",
    # `out`/`release`（`R102-35`）：shell 的构建产物目录，各自 .gitignore 挡着 —— 从前
    # 判据读进 gitignore 的构建树，同一份码在干净 clone 打 427、在本机打 446。
    "out",
    "release",
}


_file_walk_cache: dict[tuple[str, ...], list[pathlib.Path]] = {}


def iter_files(*suffixes: str) -> list[pathlib.Path]:
    """Repo files, skipping environments, caches and generated data.

    结果按 suffix 集合缓存，且遍历时**原地剪枝** IGNORED_DIRS 子树：8 个检查各调一次、
    每次全量 rglob（frontend/node_modules 几万文件照走，只是最后被过滤）曾把整份脚本
    拖到 17s（门禁耗时盘点）。目录在单次运行内不会变，缓存 + 剪枝都是纯收益。
    """
    key = tuple(sorted(suffixes))
    cached = _file_walk_cache.get(key)
    if cached is not None:
        return cached
    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 原地剪枝：巨树（node_modules / .venv / data …）整个不进入，而不是进入后再过滤。
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            if suffixes and pathlib.Path(name).suffix not in suffixes:
                continue
            found.append(pathlib.Path(dirpath) / name)
    _file_walk_cache[key] = found
    return found


def strip_comments(text: str) -> str:
    """Remove SQL (`--`) and Python (`#`) comment lines and trailing comments.

    Needed by the domain-isolation check: prose *about* a concept must not be mistaken for
    a definition *of* it. Writing "there is no user table here" kept tripping it.
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(("--", "#")):
            continue
        lines.append(line.split("--", 1)[0].split("#", 1)[0])
    return "\n".join(lines)


def out(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
    print(f"{'OK  ' if ok else 'FAIL'} {label}{(' :: ' + detail) if detail else ''}")
