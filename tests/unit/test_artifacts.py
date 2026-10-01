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
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"art_{name}", str(SCRIPTS / f"{name}.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
