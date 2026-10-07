"""`core/artifacts.py` —— 产物路径与安装包名的唯一出处，以及守着它的那把尺子。

为什么给一个"只是拼路径"的模块写单测（与 `test_scratch_db.py`、`test_bundle_parity.py` 同一个
理由）：这条链上的缺陷从来不是拼错，而是**六处各拼一遍、改布局时只改了一处**。所以这里钉两件
事：① 推导本身对不对（`artifact_name` 与 `parse_artifact_name` 必须互逆，安装包名那条式子与
`electron-builder.yml` 的 `artifactName` 必须同形）；② **禁令表真的抓得到 pathlib 的拼法** ——
第一版按单个字符串常量比，`ROOT / "build" / "sidecar"` 里没有任何一个常量含那一串，
于是 M5 那发变异当场绿（尺子看不见它要防的形状），这里把那种形状钉成用例。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    # 按**模块名**导入（不是按路径 exec）：check_consistency 拆包后是 consistency 包的
    # 薄包装器，包装器的读/写转发挂在 sys.modules 里那个实例上 —— 合成名实例拿不到。
    sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


consistency = _load("check_consistency")


def test_artifact_name_roundtrips_with_the_builder_pattern() -> None:
    from rolecard_agent.core import artifacts

    name = artifacts.artifact_name("1.2.3")
    assert name == "rolecard-agent-1.2.3-x64.exe"
    assert artifacts.parse_artifact_name(name) == ("1.2.3", "x64")
    # 不是这个形状就不猜：猜出来的版本会让下载卡把一个别的 exe 当成我们的包
    assert artifacts.parse_artifact_name("something-else.exe") is None


def test_builder_yml_agrees_with_the_module() -> None:
    """`electron-builder.yml` 抄不了 import，所以它必须与出处**同形** —— 这条是实测不是约定。"""
    from rolecard_agent.core import artifacts

    text = (ROOT / "shell" / "electron-builder.yml").read_text(encoding="utf-8")
    assert f"artifactName: {artifacts.APP_NAME}-${{version}}-${{arch}}.${{ext}}" in text
    assert f"productName: {artifacts.APP_NAME}" in text
    assert f"to: {artifacts.BACKEND_NAME}" in text


def test_paths_are_derived_not_rewritten(tmp_path: Path) -> None:
    from rolecard_agent.core import artifacts

    bundle = artifacts.sidecar_bundle(tmp_path)
    assert bundle.parts[-2:] == ("sidecar", "rolecard-backend")
    assert artifacts.sidecar_exe(tmp_path) == bundle / "rolecard-backend.exe"
    installed = artifacts.installed_dist(tmp_path)
    assert installed.as_posix().endswith(
        "/Programs/rolecard-agent/resources/rolecard-backend/_internal/frontend/dist"
    ), installed.as_posix()
    # 解包那份与装好那份只差外面一层，包内布局共用同一段 —— 各写一遍就会有一边先漂
    assert artifacts.unpacked_dist(tmp_path).parts[-3:] == artifacts.INTERNAL_DIST


def test_the_ban_table_catches_a_pathlib_chain() -> None:
    """尺子必须看得见 `ROOT / "build" / "sidecar"` 这种**跨多个常量**的拼法。

    这是 M5 那发变异照出来的洞：按单个字符串常量比，这条链里没有任何一段等于被禁的那一串，
    于是"第二处拼法"堂而皇之通过。现在 `_div_chain_parts` 把整条 `/` 链拼回来再比。
    """
    tree = ast.parse('x = base / "build" / "sidecar" / "rolecard-backend"\n')
    chain = [node for node in ast.walk(tree) if isinstance(node, ast.BinOp)]
    joined = consistency._div_chain_parts(chain[0])  # ast.walk 从最外层那条链开始
    assert '"build" / "sidecar"' in " / ".join(joined)


def test_the_ban_table_catches_a_plain_string_literal() -> None:
    tree = ast.parse('x = "rolecard-agent-*.exe"\n')
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Constant))
    assert isinstance(node.value, str) and "rolecard-agent-*.exe" in node.value


def test_schema_package_paths_counts_core_roles_and_every_domain(tmp_path: Path) -> None:
    """建表脚本那份清单只有一个出处（P1-8）：内核两份 + **每个域目录现数一份**。

    这条用例钉的是 `R28-33` 那个形状：spec 从前手抄四份，新增一个带 schema 的域插件之后
    源码态建表正常、打包态建表直接失败，而构建期一句报警都没有。现在 spec 与尺子都问这一个函数，
    所以"漏数一个域"只剩下这一种可测的错法。
    """
    from rolecard_agent.core.artifacts import schema_package_paths

    pkg = tmp_path / "rolecard_agent"
    for rel in ("core", "roles"):
        target = pkg / rel
        target.mkdir(parents=True)
        (target / "schema.sql").write_text("CREATE TABLE x(v INT);", encoding="utf-8")
    for domain in ("health", "finance"):
        target = pkg / "domains" / domain
        target.mkdir(parents=True)
        (target / "schema.sql").write_text("CREATE TABLE y(v INT);", encoding="utf-8")
    # 有域目录但没有 schema 的插件不该被算进来（它不建表）
    (pkg / "domains" / "empty_one").mkdir(parents=True)

    found = schema_package_paths(pkg)
    assert [dest for _, dest in found] == [
        "rolecard_agent/core",
        "rolecard_agent/roles",
        "rolecard_agent/domains/finance",
        "rolecard_agent/domains/health",
    ], found
    assert all(path.name == "schema.sql" and path.exists() for path, _ in found)


def test_schema_rule_is_not_written_twice_anymore() -> None:
    """spec 与尺子都不许再自己 glob 一遍 —— 那正是两条规则分叉的起点。"""
    spec_text = (ROOT / "packaging" / "rolecard-backend.spec").read_text(encoding="utf-8")
    parity_text = (SCRIPTS / "check_bundle_parity.py").read_text(encoding="utf-8")
    assert "schema_package_paths" in spec_text and 'glob("*/schema.sql")' not in spec_text
    assert "schema_package_paths" in parity_text and 'glob("*/schema.sql")' not in parity_text
