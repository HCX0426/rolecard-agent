# -*- mode: python ; coding: utf-8 -*-
"""随包 OCR worker 的 PyInstaller 配置 —— 让装机版也有本地文字识别。

**为什么要单独打一个产物，而不是把 `.venv-ocr` 拷进包**：那是一份 venv，它的
`python.exe` 依赖构建机上那个 base 解释器（本机绑在 uv standalone 的 3.13.15 上）。
拷过去等于把"只有我这台机器成立的前提"发给装机的人 —— 与第六条/第十条规矩同源
（判据与产物里不许藏着只在一台机器上成立的事实）。PyInstaller 的 onedir 自包含，
和随包后端同一个性质。

**为什么不让后端 in-process import rapidocr**：`requirements-ocr.txt` 里那条现行理由成立 ——
主服务进程的运行树里不该有 cv2(113 MB)/omegaconf 这一族，而 worker 的协议本来就是
"子进程 + argv[1] 图片路径 + stdout 文本"（换后端时一个字没改过）。打成 exe 之后协议不变，
只是 `python ocr_worker.py` 换成 `ocr-worker.exe`。

缺包就**拒绝出产物**并打出该跑的那条命令（与后端 spec 的 `RUNTIME_PACKAGES` 同一道闸）：
`collect_submodules()` 对没安装的包返回空列表且不报错，少装一族 = 安静地打出一个跑不起来的包。
"""

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # <repo>/packaging/ → 仓库根
WORKER = ROOT / "scripts" / "ocr_worker.py"

#: 运行树里必须在的族（缺一条就不出产物，理由见文件头）。
RUNTIME_PACKAGES = ("rapidocr", "onnxruntime", "cv2", "numpy", "PIL")

_missing = [pkg for pkg in RUNTIME_PACKAGES if importlib.util.find_spec(pkg) is None]
if _missing:
    msg = (
        "\n随包 OCR worker 拒绝出产物：当前解释器里没有 "
        + ", ".join(_missing)
        + "\n  这一份必须用【独立 OCR venv】的 python 来打，装齐运行依赖与打包器：\n"
        "    .venv-ocr\\Scripts\\python.exe -m pip install -r requirements-ocr.txt\n"
        "    .venv-ocr\\Scripts\\python.exe -m pip install -r requirements-package-ocr.txt\n"
        f"  现在跑的是：{sys.executable}\n"
    )
    raise SystemExit(msg)

hiddenimports: list[str] = []
datas: list[tuple[str, str]] = []
for pkg in RUNTIME_PACKAGES:
    hiddenimports += collect_submodules(pkg)
    # rapidocr 的 onnx 模型、默认配置都是"运行时读的文件"，不是 import。
    datas += collect_data_files(pkg)

a = Analysis(
    [str(WORKER)],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff", "mypy", "IPython", "matplotlib", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ocr-worker",
    debug=False,
    strip=False,
    upx=False,
    # console=True：worker 的 stdout 是**协议的一部分**（识别出的文本），
    # 打成 windowed 会让 sys.stdout 变 None，父进程拿到的永远是空串。
    console=True,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ocr-worker")
