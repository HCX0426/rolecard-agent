"""打包这条链上那几个**名字与路径**的唯一出处。

为什么要有这个模块（10-01，打包链台账 `R28-59`）：同一条事实此前写在六处 ——
`build/sidecar/rolecard-backend` 这条路径由 `build_sidecar.py`、`check_bundle_parity.py`、
`probe_package_artifact.py`、`install_package.ps1`、
`shell/electron-builder.yml`、`.github/workflows/ci.yml` 各拼一遍；
装后那条 `_internal\\frontend\\dist` 由 ps1、probe、`scripts/baseline.py` 各拼一遍；
安装包文件名模式由
`electron-builder.yml`、`install_package.ps1`、`shell_release.py`、`ci.yml` 各写一遍。
漂移不会自己说话：`artifactName` 改了只有 ps1 那条会红（它按名字找安装包），而下载卡那一格
按 glob 找 —— 它会安静地把新包当"没有包"，界面上那个下载入口直接消失，
而"入口只在真有产物时渲染"本来是设计意图（不造死按钮），于是缺陷长得像正常行为。

分层上这是 `core` 的最底一层：纯 stdlib、不读环境之外的任何东西、不碰磁盘。
`api/routers/shell_release.py` 与 `scripts/*` 都能用它（scripts 那边照本仓惯例
`sys.path.insert(0, src)`），`electron-builder.yml` 与 `.ps1` 用不了 import ——
那两处仍要写字面量，由门禁的 `artifact single source` 那条断言问它们**是否与这里一致**。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 应用名。它同时是数据根目录名、安装目录名与安装包文件名前缀 —— 一处改，处处改。
APP_NAME = "rolecard-agent"

#: 随包后端的目录名与可执行文件名（PyInstaller 的 `name=`、electron-builder 的 `to:`、
#: 装后 `resources/<这里>/` 三处本来就是同一个东西）。
BACKEND_NAME = "rolecard-backend"

#: sidecar 的产出位置（相对仓库根）。`--distpath` 给它，PyInstaller 会再套一层名字。
SIDECAR_DIR = "build/sidecar"
#: PyInstaller 的中间产物（清盘时它是最大那一坨缓存）。
SIDECAR_WORK_DIR = "build/sidecar-work"
#: electron-builder 的输出目录（相对 `shell/`）。
RELEASE_DIR = "release"
#: electron-builder `--dir` 那一份解包产物（"打包态但不装机"就读这里）。
UNPACKED_DIR = "win-unpacked"

#: 装到用户机器上的位置：`perMachine: false` ⇒ `%LOCALAPPDATA%\Programs\<APP_NAME>`。
INSTALL_SUBDIR = ("Programs", APP_NAME)
#: 包内后端的相对位置（装后）：`resources/rolecard-backend/`。
RESOURCES_DIR = "resources"

#: 安装包文件名**匹配**用的 glob。故意比式子宽一档：`artifactName` 改成不带 arch 时，
#: 窄 glob 会让下载卡找不到文件而"照设计"消失 —— 那看起来像正常行为，其实是我把缺陷藏起来。
ARTIFACT_GLOB = f"{APP_NAME}-*.exe"
_VERSION_IN_NAME = re.compile(rf"^{re.escape(APP_NAME)}-(\d+\.\d+\.\d+)-([A-Za-z0-9_]+)\.exe$")


def sidecar_bundle(root: Path) -> Path:
    """`scripts/build_sidecar.py` 的产物目录（里面是 `rolecard-backend.exe` + `_internal/`）。"""
    return root / SIDECAR_DIR / BACKEND_NAME


def sidecar_exe(root: Path) -> Path:
    """刚构建出来、还没进安装包的那份后端可执行文件。"""
    return sidecar_bundle(root) / f"{BACKEND_NAME}.exe"


#: 包内布局里"界面产物"那一段：PyInstaller 把 datas 收在 `_internal/` 下，我们那份落在
#: `_internal/frontend/dist`。装后的与解包的**共用这一段** —— 各写一遍就会有一边先漂。
INTERNAL_DIST = ("_internal", "frontend", "dist")


#: 内核那两份建表脚本所在子包（它们是本仓的骨架，不会新增）。域那一份按文件系统现数。
CORE_SCHEMA_DIRS = ("core", "roles")


def schema_package_paths(pkg: Path) -> list[tuple[Path, str]]:
    """建表脚本 → 包内相对路径的清单，**spec 与那条 parity 尺子共用这一份**。

    从前两处各写一遍同样的 glob（`packaging/rolecard-backend.spec` 与
    `scripts/check_bundle_parity.py`）：`R28-33` 那次把 spec 里手抄的四份改成"域目录现数"，
    尺子那边跟着改了第二次 —— 两条规则一旦分叉，症状是"源码态建表正常、打包态建表直接失败"
    而构建期一句报警都没有。运行时读它的是 `storage/db.py:domain_schema_path()`，
    口径同样是"域目录里有 schema.sql 就算一份"。
    """
    found: list[tuple[Path, str]] = [
        (pkg / rel / "schema.sql", f"rolecard_agent/{rel}")
        for rel in CORE_SCHEMA_DIRS
        if (pkg / rel / "schema.sql").exists()
    ]
    domains = pkg / "domains"
    if domains.is_dir():
        found += [
            (sql, f"rolecard_agent/domains/{sql.parent.name}")
            for sql in sorted(domains.glob("*/schema.sql"))
        ]
    return found


def unpacked_backend(shell_release_dir: Path) -> Path:
    """`electron-builder --dir` 解包产物里的后端目录（打包态、未安装）。"""
    return shell_release_dir / UNPACKED_DIR / RESOURCES_DIR / BACKEND_NAME


def unpacked_dist(shell_release_dir: Path) -> Path:
    """解包那份里的界面产物 —— 与 `installed_dist` 同一段包内布局，只差外面那层目录。"""
    return unpacked_backend(shell_release_dir).joinpath(*INTERNAL_DIST)


def installed_dir(local_appdata: Path) -> Path:
    """装到这台机器上的应用目录（Windows 装机形态）。"""
    return local_appdata.joinpath(*INSTALL_SUBDIR)


def installed_backend_bundle(local_appdata: Path) -> Path:
    """装好的那份后端目录 —— `probe_package_artifact.py` 与装机脚本读的同一个位置。"""
    return installed_dir(local_appdata) / RESOURCES_DIR / BACKEND_NAME


def installed_dist(local_appdata: Path) -> Path:
    """装好的那份**界面产物**（`_internal/frontend/dist`）。

    这串路径从前在三个地方各拼一遍，于是"改一次包内布局就要在三处对齐"，
    而不对齐的代价是读到一个空目录 —— 空目录看起来像"没装界面"，不像"我拼错了"。
    """
    return installed_backend_bundle(local_appdata) / Path(*INTERNAL_DIST)


def artifact_name(version: str, arch: str = "x64", ext: str = "exe") -> str:
    """安装包文件名 —— 与 `electron-builder.yml` 的 `artifactName` 同一条式子。"""
    return f"{APP_NAME}-{version}-{arch}.{ext}"


def parse_artifact_name(name: str) -> tuple[str, str] | None:
    """从文件名里读 `(version, arch)`；不是这个形状就回 None，不猜。"""
    match = _VERSION_IN_NAME.match(name)
    if not match:
        return None
    return match.group(1), match.group(2)
