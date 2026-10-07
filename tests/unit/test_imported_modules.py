"""`_imported_modules` 走 AST 之后的行为（P3-9「解析器 AST 化」这一格的尺子）。

钉的是**正则版做不到、或做错**的那几种写法：相对导入、`as` 别名、括号续行的
from-import、缩进块里的 import、以及**字符串里的假 import**（正则会把
`s = "import fake"` 里的 fake 当成真模块报出来）。这些不是"顺手多测几条"，而是
这条尺子的判据本身 —— 它要的是"被 import 的**模块**"，不是"文本里长得像 import 的词"。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_consistency as cc  # noqa: E402


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # 老用例（正则版就对的）—— 保底不许回归
        ("import os", ["os"]),
        ("import json,sys", ["json", "sys"]),
        (
            "from rolecard_agent.config import DEFAULT_X; print(1)",
            ["rolecard_agent.config"],
        ),
        # 符号不是模块：from-import 只认 module，不认 import 的那一串名字
        ("from x import y as z", ["x"]),
        ("from a import (b, c)", ["a"]),
        # 相对导入没有可判的模块名
        ("from . import core", []),
        ("from .core import ROOT", ["core"]),
        # 别名取的是模块原名，不是别名
        ("import numpy as np", ["numpy"]),
        # 缩进块里的 import 也是 import
        ("if True:\n    import sys", ["sys"]),
        # 字符串里的假 import 不算
        ('s = "import fake"', []),
        ('s = "from fake_pkg import thing"', []),
    ],
)
def test_只报模块不报符号(src: str, expected: list[str]) -> None:
    assert cc._imported_modules(src) == expected


def test_ci_yml里带缩进与外壳引号的那一段照样判得出来() -> None:
    """调用方喂的是 `python3 -c` 后面**整段**（含缩进与 YAML/shell 的外层引号括号）。

    第一版 AST 化在这里静默解析失败并返回 []，把一支"必须红"的用例变成了绿 ——
    这条钉住：噪声不许把判据变成摆设。
    """
    line = (
        '      SF=$(python3 -c "from rolecard_agent.config import '
        'DEFAULT_SILICONFLOW_BASE_URL; print(DEFAULT_SILICONFLOW_BASE_URL)")'
    )
    code = line.split("-c", 1)[1]
    assert cc._imported_modules(code) == ["rolecard_agent.config"]
