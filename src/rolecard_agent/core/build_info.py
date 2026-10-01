"""这一份后端是**从哪个 commit 打出来的** —— "装的是哪一版"这句话唯一的机器可读答案。

为什么要它（10-01 那轮打包链盘点，台账 `R28-56`）：`probe_package_artifact.py` 原本有三层
判据 —— ① 比前端产物逐文件哈希、② 比刚构建的 exe 字节、③ 起包内那个 exe 问一个
"只可能来自新代码"的读数。一次**纯后端**改动会让 ① 一字不差地绿（dist 真没变），而 ② 在没有
`build/sidecar` 时只能跳过，③ 问的那个键（`dangling`）从第十五包里就已经存在 ——
三层全绿也证明不了"跑着的后端
是最新的那一笔"。09-26 那次就是靠人肉读一句说明文字才判出来的（台账里写着）。把事实**烤进产物**，
三层就塌成一次对比，而且不再依赖"比对对象还在不在"。

两条读法：

* **冻结态**读打包时烤进 `_internal/build_info.json` 的那一份 —— 那是"这一包是从哪份源码打的"
  的原始记录，升级覆盖整个目录也不会错。
* **开发态**现取 `git rev-parse HEAD` 加"工作树脏不脏"，因为开发态跑的就是工作树。

**读不到就回 `unknown`，绝不猜**：判据宁可红在"这一发证明不了"，也不要绿在"看起来对"
（与本仓"未知不拦"相反的那一半 —— 那条管的是"别把不确定的事拦下来"，而这里的不确定
必须由**验货的人**看见，不是由被验的东西掩饰）。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import bundle_root, is_frozen, repo_root

#: 烤进产物的那份文件叫什么（`packaging/rolecard-backend.spec` 与 `scripts/build_sidecar.py`
#: 都从这里取名字，两处各写一遍就会有"打了但读不到"的单边瞎）。
BUILD_INFO_NAME = "build_info.json"
#: 指纹长度：够唯一定位一个 commit，又不至于把整条 sha 摊到免鉴权的健康接口上。
FINGERPRINT_LENGTH = 12
#: 证明不了时的取值 —— **不是空串**，因为空串会被"两边都空所以相等"骗过去。
UNKNOWN = "unknown"


def build_info_path() -> Path:
    """构建信息文件的位置：冻结态在 `_internal/` 根，开发态在仓库的 `build/` 下。"""
    if is_frozen():
        return bundle_root() / BUILD_INFO_NAME
    return repo_root() / "build" / BUILD_INFO_NAME


def _git(*args: str) -> str:
    """问 git 要一份身份；**问不到就回空串**（调用方把它翻译成 `unknown`）。

    这里必须自己接住 `FileNotFoundError`：镜像里没有 `git` 这个二进制（`python:3.13-slim` 不带，
    `.dockerignore` 也不带 `.git`），10-01 CI 的镜像那一臂就是被这个异常打出来的 ——
    那条探活接口抛 500、Dockerfile 里的容器探针永远不通过、编排器眼里这个容器
    "活着但没就绪"。
    本模块的规矩从头是"**问不到就说问不到**"（`read_build_info` 不许抛），而第一版只接住了
    返回码非零与文件不存在两种，漏了"根本起不动子进程"这一种。
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=str(repo_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # OSError 覆盖 FileNotFoundError（没装 git）与 PermissionError；
        # SubprocessError 覆盖 timeout —— 一条探活接口不该为身份这件事等出一个 500。
        return ""
    return result.stdout.strip()


@dataclass(frozen=True)
class BuildInfo:
    """一次运行的身份：哪个 commit、什么时候打的、当时工作树脏不脏。"""

    git_sha: str = UNKNOWN
    built_utc: str = UNKNOWN
    dirty: bool | None = None

    @property
    def fingerprint(self) -> str:
        """给探活接口与验货判据用的短指纹。"""
        return self.git_sha[:FINGERPRINT_LENGTH] if self.git_sha != UNKNOWN else UNKNOWN

    @property
    def known(self) -> bool:
        return self.git_sha != UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {"sha": self.fingerprint, "built": self.built_utc, "dirty": self.dirty}


@lru_cache(maxsize=1)
def _dev_identity() -> tuple[str, bool | None]:
    """开发态现取：HEAD 的 sha + 工作树是否脏。

    一个进程算一次（`lru_cache`）：健康接口会被壳轮询，而开发态的 HEAD 不会在你眼前变。
    脏这件事**必须说出来** —— 从脏工作树打出来的包，"装的就是 HEAD"那句话本来就是假的。
    问不到 HEAD（镜像里没 git）时脏旗回 **None** 而不是 False：「没记」与「记了说干净」
    是两件事（同 `R28-66` 那条口径），把"问不到"写成"干净"是最省事的假绿。
    """
    sha = _git("rev-parse", "HEAD")
    if not sha:
        return UNKNOWN, None
    return sha, bool(_git("status", "--porcelain"))


def read_build_info() -> BuildInfo:
    """这份代码的身份。读不到不抛错，回 `unknown` —— 让判据自己去红。"""
    if not is_frozen():
        sha, dirty = _dev_identity()
        built = UNKNOWN if sha == UNKNOWN else "working-tree"
        return BuildInfo(git_sha=sha, built_utc=built, dirty=dirty)
    path = build_info_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return BuildInfo()
    sha = str(raw.get("git_sha") or "")
    if len(sha) < FINGERPRINT_LENGTH:
        return BuildInfo()
    dirty = raw.get("dirty")
    return BuildInfo(
        git_sha=sha,
        built_utc=str(raw.get("built_utc") or UNKNOWN),
        dirty=dirty if isinstance(dirty, bool) else None,
    )
