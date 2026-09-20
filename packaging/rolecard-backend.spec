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
# 建表脚本：目标目录 = 运行时 PACKAGE_ROOT 之下那个相对位置，一字不能错。
for rel in ("core", "roles", "domains/health", "domains/finance"):
    sql = PKG / rel / "schema.sql"
    if sql.exists():
        datas.append((str(sql), f"rolecard_agent/{rel}"))

hiddenimports = collect_submodules("rolecard_agent")
for pkg in ("uvicorn", "chromadb", "trafilatura", "langchain_core", "langchain", "langgraph"):
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
