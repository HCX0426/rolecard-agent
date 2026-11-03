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


def test_parse_artifact_name_matches_the_shared_vectors() -> None:
    """两侧（python 与壳的 TS）对同一个名字必须给出**同一个**答案。

    为什么拿一张 JSON 表而不是各自写断言（2026-11-03 审查 A-1）：安装包名的解析在仓里是
    **两份实现**——`core/artifacts.py` 的正则与 `shell/main/updateCacheCleanup.ts` 的正则，
    跨语言 import 不到，于是各自的测试只能各自绿。实测两份**都坏在同一个输入上**：版本段
    写成三段不含点，而仓库当时的版本号正是 `0.5.1-beta.1` ⇒ 现网名字解析成 None ⇒
    "已装完就清掉 pending"那条判据在 beta 上永久静默失效（日志写"解析不出版本"，
    看起来像"正常，没东西可清"）。同一天还量出第二处分叉：`rolecard-agent-0.4.2.exe`
    （不带 arch）TS 侧解析成功、Python 侧返回 null，而两侧注释都自称"同一宽容口径"。

    判据因此搬到两侧都能读的这张表里：`config/artifact-name-vectors.json`。
    壳侧那份测试读**同一个文件**（见 shell/scripts/test-update-cache-cleanup.cjs）。
    谁改了正则而另一侧没跟上，就有一边红 —— 这才是"两条实现、一个事实"的可执行形态。
    """
    import json

    from rolecard_agent.core import artifacts

    vectors_path = ROOT / "config" / "artifact-name-vectors.json"
    assert vectors_path.exists(), f"共享向量表不见了：{vectors_path}"
    vectors = json.loads(vectors_path.read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 8, f"向量表只剩 {len(vectors)} 条 —— 覆盖塌了，不许静默变绿"

    for vec in vectors:
        name = vec["name"]
        parsed = artifacts.parse_artifact_name(name)
        if vec["version"] is None:
            assert parsed is None, f"{name} 应当不认，却解析成 {parsed}"
        else:
            assert parsed == (vec["version"], vec["arch"]), f"{name} -> {parsed}"

    # 现网那一份产物名必须在表里（A-1 就是栽在"测试用的形状"与"真实产物的形状"不是一回事）
    live = artifacts.artifact_name(_current_version())
    assert any(v["name"] == live for v in vectors), f"现网产物名 {live} 不在向量表里"
    assert artifacts.parse_artifact_name(live) is not None, f"现网名字 {live} 解析不出 —— A-1 复发"


def _current_version() -> str:
    """版本号只读一处（`pyproject.toml`，门禁的 version parity 管那四处一致）。"""
    import tomllib

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["project"]["version"]


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
