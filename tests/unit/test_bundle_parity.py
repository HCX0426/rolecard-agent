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


def test_a_missing_bundle_raises_instead_of_returning_empty(tmp_path: Path) -> None:
    """产物不存在就抛。静默返回空集会让"包里一个模块都没有"读成"全都缺席"，或者更糟：读成"没什么可查"。

    这一条**不碰 PyInstaller**（存在性判断在它之前，见脚本里那行注释）—— CI 的 venv 里没有打包工具，
    09-29 那次推上去就是红在这个假设上：脚本自己的教训，测试不能再来一遍。
    """
    with pytest.raises(FileNotFoundError):
        mod.bundle_top_names(tmp_path / "不存在的包")


def test_an_unreadable_archive_raises(tmp_path: Path) -> None:
    """bundle 在、archive 读不出 ⇒ 也要抛，不能回空集。要 PyInstaller，缺了就带理由 skip。"""
    reason = (
        "只有装过打包工具的构建机能量这条"
        "（CI 的 ubuntu runner 不装 PyInstaller，它从不打 Windows 包）"
    )
    pytest.importorskip("PyInstaller.archive.readers", reason=reason)
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


def test_declared_schemas_follow_the_domain_directories(tmp_path: Path) -> None:
    """新增一个带 schema 的域目录 ⇒ 声明侧自己长出来，不靠人手抄进 spec（`R28-33`）。"""
    pkg = tmp_path / "src" / "rolecard_agent"
    for rel in ("core", "roles", "domains/health", "domains/finance"):
        _write(pkg / rel / "schema.sql", "CREATE TABLE t (x INTEGER);")
    before = mod.declared_schema_files(tmp_path / "src")
    assert before == [
        "rolecard_agent/core/schema.sql",
        "rolecard_agent/roles/schema.sql",
        "rolecard_agent/domains/finance/schema.sql",
        "rolecard_agent/domains/health/schema.sql",
    ], before

    # 新域插件落一份 schema.sql 进来
    _write(pkg / "domains" / "sleep" / "schema.sql", "CREATE TABLE sleep_log (x INTEGER);")
    after = mod.declared_schema_files(tmp_path / "src")
    assert "rolecard_agent/domains/sleep/schema.sql" in after, after
    # 没有 schema.sql 的域目录不该被硬造一份出来
    (pkg / "domains" / "empty_domain").mkdir()
    again = mod.declared_schema_files(tmp_path / "src")
    assert "rolecard_agent/domains/empty_domain/schema.sql" not in again, again


def test_bundle_schema_inventory_reads_the_internal_tree(tmp_path: Path) -> None:
    """包内清单从 `_internal/` 现数。

    目录不存在时回空集是**故意的**：那份空集对着非空声明就是红，而不是"没得查"。
    """
    internal = tmp_path / "_internal" / "rolecard_agent"
    _write(internal / "core" / "schema.sql", "SELECT 1;")
    _write(internal / "domains" / "health" / "schema.sql", "SELECT 1;")
    found = mod.bundled_schema_files(tmp_path)
    assert found == {
        "rolecard_agent/core/schema.sql",
        "rolecard_agent/domains/health/schema.sql",
    }, found
    assert mod.bundled_schema_files(tmp_path / "没打过的包") == set()


def test_the_real_repo_declares_core_roles_and_every_domain() -> None:
    """真树哨兵：这条检查读的是 `src/`，路径根漂了就等于没查。"""
    root = Path(SCRIPT).resolve().parents[1]
    declared = mod.declared_schema_files(root / "src")
    assert "rolecard_agent/core/schema.sql" in declared
    assert "rolecard_agent/roles/schema.sql" in declared
    for domain_dir in (root / "src" / "rolecard_agent" / "domains").glob("*/schema.sql"):
        assert f"rolecard_agent/domains/{domain_dir.parent.name}/schema.sql" in declared


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


def test_lazy_scan_keeps_only_function_body_imports(tmp_path: Path) -> None:
    """懒加载那本账只数**函数体内**的 import：顶层那族 PyInstaller 的 import 图自己跟得到，
    把它们也要求进 RUNTIME_PACKAGES 会把清单写成一张"所有依赖表"，谁都不敢删。"""
    _write(
        tmp_path / "src" / "pkg" / "mix.py",
        """
        import top_level_pkg

        def load():
            from lazy_pkg import Thing

            return Thing
        """,
    )
    lazy = mod.lazy_third_party_imports(tmp_path / "src")
    all_imports = mod.scan_third_party_imports(tmp_path / "src")
    assert "lazy_pkg" in lazy
    assert "top_level_pkg" not in lazy, "顶层 import 不该被要求整族收集"
    assert "top_level_pkg" in all_imports, "两个扫描器的分工：一个数全部，一个只数懒的"


def test_spec_runtime_packages_reads_the_real_spec() -> None:
    """对着**真 spec** 读一次：读法本身是两处尺子共用的（门禁那条 + 这条），
    解析形状漂了必须在这里红，而不是等到某天真要补一族才发现读不出来。"""
    listed = mod.spec_runtime_packages()
    assert listed, "没从 packaging/rolecard-backend.spec 读出 RUNTIME_PACKAGES"
    assert {"langchain_mcp_adapters", "mcp", "chromadb"} <= set(listed), listed
    assert all(isinstance(item, str) for item in listed)


def test_lazy_family_absent_from_the_spec_list_is_red(tmp_path: Path) -> None:
    """M2 那发变异的形状：有人把一族从清单里摘掉，而 src 还在函数体里 import 它。

    这条是 10-01 新加的**反向**判据。旧尺子只问"src import 的每族在不在已打好的包里"，
    那需要产物在场，等于"打完才醒一次"；而摘掉清单的一族在包里可能还在（靠 import 图），
    于是数据文件与子模块少收这件事**谁都看不见**。
    """
    gaps = mod.missing_from_spec({"some_lazy_pkg": "src/pkg/x.py:4"}, ["other_pkg"])
    assert gaps == {"some_lazy_pkg": "src/pkg/x.py:4"}
    assert mod.missing_from_spec({"other_pkg": "src/pkg/x.py:4"}, ["Other_Pkg"]) == {}


def test_import_graph_exception_needs_a_non_empty_reason() -> None:
    """"这族靠 import 图收得到"也要署名写理由，空理由不给绿 —— 与 NOT_BUNDLED_BY_DESIGN 同一条纪律。

    两本账不能合成一本：一本说"刻意不进包"，一本说"进包但不必整族收"。
    """
    allow = {"httpx": "实测在 bundle 顶层名里，纯 Python 无数据文件"}
    assert mod.missing_from_spec({"httpx": "src/pkg/x.py:4"}, [], allow=allow) == {}
    blank = mod.missing_from_spec({"httpx": "src/pkg/x.py:4"}, [], allow={"httpx": "   "})
    assert blank, "空理由等于没登记，这条必须还是缺席"
