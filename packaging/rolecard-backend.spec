# -*- mode: python ; coding: utf-8 -*-
"""随包后端（sidecar）的 PyInstaller 配置 —— 里程碑 D②-4。

**为什么 onedir 而不是 onefile**：onefile 每次启动都要把整个包解到临时目录（这一坨几百 MB，
冷启动几秒起），而且"自解压再执行"正是杀毒软件最爱报的行为。onedir 装完直接跑，安装包压缩
后总体积几乎没差。

三类东西必须显式登记，都不是"PyInstaller 不够聪明"，而是这套依赖的性质：

1. `rolecard_agent.api.main` 是 uvicorn **用字符串**在运行时 import 的（`create_app` 工厂），
   静态分析看不见 → 整个包 collect_submodules。
2. `*.sql` 是运行时读的文件不是 import → add-data，且目标路径必须和 `storage/db.py` 里
   `PACKAGE_ROOT` 的相对位置一致（`rolecard_agent/<...>/schema.sql`），否则建表直接失败。
3. `frontend/dist` 同理：控制台界面是后端**静态托管**的一堆文件，不是代码。

chromadb / uvicorn / trafilatura 各有动态导入与自带数据目录，用 hooks-contrib 的收集器兜住。
"""

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # <repo>/packaging/ → 仓库根
PKG = ROOT / "src" / "rolecard_agent"
DIST = ROOT / "frontend" / "dist"

# **这一行是必需的，不是风格**：本项目是 src-layout 且没有 `pip install -e`，所以
# `rolecard_agent` 对打包进程本身不可导入。`pathex` 只喂 Analysis 的模块图，
# `collect_submodules()` 走的是当前解释器的 import 系统 —— 少了这行它会**安静地返回空**，
# 于是一个模块都没被打进包，表现是 exe 起来了却报
# `Error loading ASGI app. Could not import module "rolecard_agent.api.main"`。
sys.path.insert(0, str(ROOT / "src"))

if not (DIST / "index.html").exists():
    msg = f"先构建前端再来打包后端：{DIST} 不存在（cd frontend && npm run build）"
    raise SystemExit(msg)

datas = [(str(DIST), "frontend/dist")]
# **身份也要打进包**（10-01，台账 R28-56）：`core/build_info.py` 在冻结态读
# `_internal/build_info.json` 才知道"这一包是从哪个 commit 打的"，`/api/health` 于是能报指纹，
# 第③层就从"问一个恰好只在新代码里存在的键"升级成"直接定版"。
# 缺这个文件就**拒绝出产物** —— 一个不知道自己是谁的包，正是 09-26 那次"纯后端改动、前端哈希
# 一字不差、验货照样打 OK"的形状。正常入口 `scripts/build_sidecar.py` 一定会先写它；
# 会撞到这条的只有"直接 pyinstaller 这个 spec"的人，而那条路本来就该被拦。
BUILD_INFO = ROOT / "build" / "build_info.json"
if not BUILD_INFO.exists():
    raise SystemExit(
        f"缺 {BUILD_INFO} —— 打出来的包会没有身份（`/api/health` 只能报 unknown）。"
        "请走 `python scripts/build_sidecar.py`，它会先写指纹再调本 spec。"
    )
datas.append((str(BUILD_INFO), "."))
# 建表脚本：目标目录 = 运行时 PACKAGE_ROOT 之下那个相对位置，一字不能错。
# 内核那两份写死（它们是本仓的骨架，不会新增）；**域那一份按文件系统现数**
# （审计 `R28-33`）：运行时走的是 `storage/db.py:domain_schema_path()` 的
# `domains/<id>/schema.sql`，是"域目录里有 schema 就建表"的动态口径。spec 原来手抄四份，
# 于是新增一个带 schema 的域插件 ⇒ 源码态建表正常、**打包态建表直接失败**，
# 而构建期一句报警都没有。数文件这件事只该有一个出处。
# **数文件这件事只该有一个出处**（10-01 台账 R28-59）：从前 spec 与 `check_bundle_parity.py` 各写
# 一遍同样的 glob，两条规则一分叉，症状又是「源码态建表正常、打包态建表直接失败」而构建期零报警。
# 现在两边都问 `core/artifacts.py::schema_package_paths`（运行时的口径是 db.py:domain_schema_path()，
# 三处本来就是同一件事）。
from rolecard_agent.core.artifacts import schema_package_paths  # noqa: E402 - 上面刚插过 sys.path

for _sql_file, _dest in schema_package_paths(PKG):
    datas.append((str(_sql_file), _dest))

# **域包的 `__init__.py` 必须落盘，不是可选项**（2026-10-09 v0.3.0 发布链第③处"首次实跑
# 才暴露"）：PyInstaller 把纯模块收进 PYZ 归档（不落盘），而 `domains/registry.py::
# _domain_dirs()` 在运行时**扫文件系统**找域目录（`iterdir()` + 查 `__init__.py`）——
# 于是冻结态每个域目录里只剩 schema.sql 数据文件，`__init__.py` 在归档里 ⇒ 每个域都被判
# 「不是包（缺 __init__.py）」，exe 起来就退（本地源码态永远复现不出来：那边扫的是真源码树）。
# schema.sql 走 add-data 是既有机制，这里把**包标记文件**同一口径收进同一路径。
# 首版传错根（把 `src/rolecard_agent` 传给了只认 `domains/` 的 `_domain_dirs` ⇒ 扫出
# api/base/core… 八个顶层包、一个域都没收）—— 函数自己的默认值 `_PACKAGE_DIR` 就是
# domains/，不传根才是对的用法。
from rolecard_agent.domains import registry as _domain_registry  # noqa: E402

for _domain_dir in _domain_registry._domain_dirs():
    _init = _domain_dir / "__init__.py"
    if _init.is_file():
        datas.append((str(_init), f"rolecard_agent/domains/{_domain_dir.name}"))

hiddenimports = collect_submodules("rolecard_agent")

# 这一族是**随包后端运行时真要用**的包，逐个 `collect_submodules` + 收数据目录。
# MCP 那两条 09-29 补进来（审计 `R28-34`）：`core/tools/mcp.py:69` 那句
# `from langchain_mcp_adapters.client import MultiServerMCPClient` 是**函数体内的 lazy import**，
# 而这两个包当时在构建机的 .venv 里根本没装 ⇒ 包里 0 个模块 ⇒ 打包态的 MCP 永远 fail-open
# （设置→扩展那面板照常摆着，工具永远加载不出来，只有日志里一句 warning）。
# 代价实测：langchain-mcp-adapters 0.16 MB + mcp 1.71 MB = 1.87 MB，安装包 191 MB 的 1%。
RUNTIME_PACKAGES = (
    "uvicorn",
    "chromadb",
    "trafilatura",
    "langchain_core",
    "langchain",
    "langgraph",
    "langchain_mcp_adapters",
    "mcp",
)

# **下面这段护栏是这条配置里最要紧的一行，不是装饰**：`collect_submodules()` 对没安装的包
# 返回**空列表且不报错**（spec 开头 `sys.path` 那条注释记的是同一个坑的另一半）。少装一个包 ⇒
# 安静地少收一族模块 ⇒ 打出来的包"看着成功"而里面缺东西，症状要等用户点到那格才出现。
# 与其让 parity 检查在事后红（`scripts/check_bundle_parity.py`），不如在这里就不出产物。
_missing = [pkg for pkg in RUNTIME_PACKAGES if importlib.util.find_spec(pkg) is None]
if _missing:
    raise SystemExit(
        "随包后端要收的这些包没装，拒绝出一个「缺模块」的产物：" + ", ".join(_missing)
        + "\n  装回来（**一条就够**，它自己把运行时那五族带上）："
        "\n    .venv\\Scripts\\python.exe -m pip install -r requirements-package.txt"
        "\n  别再手抄 pip 命令：这条提示从前少写 -api/-rag/-cloud，照它装完 uvicorn/chromadb"
        "\n  仍然缺席、第二次还是拒绝出产物（10-01 实测）。清单的内容只在一份文件里有一份。"
    )

for pkg in RUNTIME_PACKAGES:
    hiddenimports += collect_submodules(pkg)
    datas += collect_data_files(pkg)

a = Analysis(
    [str(ROOT / "scripts" / "run_api.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff", "mypy", "IPython", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rolecard-backend",
    debug=False,
    strip=False,
    upx=False,
    # console=True：后端的 stdout 由壳接走落盘（`userData/backend.log`）。
    # 打成 windowed 会让 `sys.stdout` 变成 None，那句启动横幅直接抛异常。
    console=True,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rolecard-backend")
