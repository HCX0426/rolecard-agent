"""依赖方向门禁：跑 pyproject 的 import-linter 契约，红就是"横向抓取回来了"。

为什么包一层而不是把 `import-linter` 直接写进 `gate.py` 的命令里（三件事都必须在这里做掉）：

  1. **编码**：Windows 上子进程的 stdout 默认按 GBK 写，而 gate 用 UTF-8 解它 —— 契约名是
     中文的，直接跑会在解码那一层变成一串问号（`gate.py` 自己重配过编码，子进程不会）。
     所以这里**同进程**跑并自己 `reconfigure`，与 `scripts/` 其余入口同一条约定。
  2. **找得到包**：本仓是 src/ 布局且不做 `pip install -e .`（与 mypy 的 `mypy_path`、
     pytest 的 `pythonpath` 同一个理由），grimp 要 import `rolecard_agent` 就得先把
     `src` 放进 `sys.path`。这一步漏掉的形状不是报错，而是"契约从没真正跑过"。
  3. **只读 TOML 那份配置**：`config_filename` 显式指向 `pyproject.toml`，否则 INI reader
     会先试 `setup.cfg` / `.importlinter`，而那个 reader 按**区域编码**开文件 —— 中文注释
     的 INI 在它手里当场 UnicodeDecodeError（实测中招过一次）。

判据与豁免纪律写在 `pyproject.toml` 的 `[tool.importlinter]`；那一段注释是唯一的解释。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 与 scripts/ 其余入口同一动作：不打 GBK 装不下的字（这里打的就是契约名）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)

    from importlinter.cli import lint_imports  # noqa: PLC0415 - 要先备好 sys.path

    return lint_imports(config_filename=str(ROOT / "pyproject.toml"), no_logo=True)


if __name__ == "__main__":
    sys.exit(main())
