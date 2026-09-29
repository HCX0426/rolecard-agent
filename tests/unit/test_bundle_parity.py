"""`scripts/check_bundle_parity.py` 的测试：那把尺子本身必须能被一次缺席照红。

为什么给一条门禁脚本写单测（与 `test_scratch_db.py` 同一个理由）：它防的是"**打出来的包安静地
缺东西**"（审计 `R28-34`）—— 而这类缺陷的症状是几个月后某个人在桌宠包里点了「设置→扩展」里那格，
发现工具永远加载不出来。尺子如果自己有洞（漏扫 lazy import、大小写把 `_internal` 目录名读成缺席、
`--allow` 那族被误用成"忘了"），它给出的就是**假绿灯**，比没有更坏。

所以这里钉四件事：函数体内的 import 也算数、stdlib/第一方不进场、缺席与"刻意不进"分得开、
读不出 bundle 时报错而不是静默返回空集。
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_bundle_parity.py"


def _load(tag: str):
    spec = importlib.util.spec_from_file_location(f"bundle_parity_{tag}", str(SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load("main")


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def test_lazy_import_inside_a_function_counts(tmp_path: Path) -> None:
    """MCP 那一条正是这个形状：`from x.y import Z` 写在函数体里，"静态看像没在用"。"""
    _write(
        tmp_path / "src" / "pkg" / "tools.py",
        """
        import os

        def load():
            from langchain_mcp_adapters.client import MultiServerMCPClient

            return MultiServerMCPClient
        """,
    )
    found = mod.scan_third_party_imports(tmp_path / "src")
    assert "langchain_mcp_adapters" in found
    assert found["langchain_mcp_adapters"].endswith("tools.py:4")  # 带行号，红的时候不用再 grep


def test_relative_and_stdlib_and_first_party_are_out(tmp_path: Path) -> None:
    """`import json` / `from .sibling import x` / `from rolecard_agent...` 都不该进缺席清单。"""
    _write(
        tmp_path / "src" / "pkg" / "one.py",
        """
        import json
        from . import sibling
        from ..core import thing
        import rolecard_agent.config

        def f():
            import sqlite3
            return json, sibling, thing, sqlite3
        """,
    )
    assert mod.scan_third_party_imports(tmp_path / "src") == {}


def test_a_truly_missing_module_is_red_and_the_reasoned_exception_is_not() -> None:
    imports = {"mcp": "src/a.py:1", "langchain_mcp_adapters": "src/b.py:2"}
    bundled = {"mcp", "anyio", "pydantic"}

    missing = mod.missing_from_bundle(imports, bundled)
    assert missing == {"langchain_mcp_adapters": "src/b.py:2"}

    # 刻意不进包 ⇒ 不红，但**必须带理由**（见下面那条测试）。只给键不给值，也算红。
    reasoned = {"langchain_mcp_adapters": "只做 dev 用"}
    assert mod.missing_from_bundle(imports, bundled, allow=reasoned) == {}
    assert mod.missing_from_bundle(imports, bundled, allow={"langchain_mcp_adapters": ""}) == {
        "langchain_mcp_adapters": "src/b.py:2"
    }


def test_names_are_compared_case_insensitively(tmp_path: Path) -> None:
    """`_internal/` 下的目录名各家风格不一（`PIL`、`PyPDF2`…），大小写敏感就是假阳性。"""
    assert mod.missing_from_bundle({"Pillow": "src/a.py:1"}, {"pillow"}) == {}


def test_an_unreadable_bundle_raises_instead_of_returning_empty(tmp_path: Path) -> None:
    """读不到就抛。静默返回空集会让"包里一个模块都没有"读成"全都缺席"，或者更糟：读成"没什么可查"。"""
    with pytest.raises(FileNotFoundError):
        mod.bundle_top_names(tmp_path / "不存在的包")

    bundle = tmp_path / "rolecard-backend"
    bundle.mkdir()
    (bundle / "rolecard-backend.exe").write_bytes(b"not a PyInstaller archive")
    with pytest.raises(Exception):  # noqa: B017 - CArchiveReader 对垃圾输入抛什么不在我们掌控内
        mod.bundle_top_names(bundle)


def test_every_by_design_exception_signs_a_reason() -> None:
    """`NOT_BUNDLED_BY_DESIGN` 是"忘了"和"想过"唯一分得开的地方：每一条必须有非空理由。"""
    for name, reason in mod.NOT_BUNDLED_BY_DESIGN.items():
        assert reason.strip(), f"「刻意不进包」的 {name} 没写理由"
        assert not reason.strip().startswith("TODO"), f"{name} 的理由还是占位"


def test_the_real_repo_tree_is_scanned_by_the_gate_script() -> None:
    """整条尺子对着**真树**跑一次不崩，且 src 真 import 的那一族被扫到了（数量级哨兵）。

    这一条防的是"脚本只在自己的 fixture 里对"：fixture 用了假 `src/`，真树里那条 lazy import
    被拼错、或 rglob 的路径根漂了，单测照样全绿。
    """
    root = Path(SCRIPT).resolve().parents[1]
    found = mod.scan_third_party_imports(root / "src")
    assert {"langchain_core", "fastapi", "chromadb"} <= set(found), sorted(found)
    assert all(where.startswith("src/") for where in found.values()), found
    assert "rolecard_agent" not in found
