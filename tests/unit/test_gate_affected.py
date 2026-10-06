"""受影响用例选择（`gate.py` 的 `select_affected`）：它省的是时间，赌的是"没跑的那些不会红"。

所以这里钉的**不是"挑得准"**，而是"挑不出来时必须退回全量"的那几条边界 —— 每一条都是
"这处改动可能牵动别处"的形状。判据朝保守一侧倒，是因为反过来的错误（挑得太窄、绿得虚假）
在这个仓库里叫"格子骗人"，比慢一分钟贵得多。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_TESTS = [
    "tests/unit/test_api.py",
    "tests/unit/test_import_floor.py",
    "tests/unit/test_memory.py",
    "tests/unit/test_ocr.py",
]


def _load_gate():
    path = ROOT / "scripts" / "gate.py"
    spec = importlib.util.spec_from_file_location("gate_affected_under_test", str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_窄改动挑出同名测试并带上常跑集() -> None:
    gate = _load_gate()
    picked, why = gate.select_affected(["src/rolecard_agent/rag/ocr.py"], _TESTS)
    assert picked == ["tests/unit/test_import_floor.py", "tests/unit/test_ocr.py"], picked
    assert "1 个模块" in why and "2/4" in why, why


def test_包顶层模块当场退回全量() -> None:
    """`config.py` 这类谁都可能 import —— 快照里点名的那一格。"""
    gate = _load_gate()
    picked, why = gate.select_affected(["src/rolecard_agent/config.py"], _TESTS)
    assert picked == []
    assert "顶层模块" in why, why


def test_地基包当场退回全量() -> None:
    """`base/` 与 `storage/` 是上下各层都 import 的地基：只跑同名测试是假的。"""
    gate = _load_gate()
    for path in ("src/rolecard_agent/base/paths.py", "src/rolecard_agent/storage/db.py"):
        picked, why = gate.select_affected([path], _TESTS)
        assert picked == [], path
        assert "地基包" in why, why


def test_映射零命中当场退回全量() -> None:
    """这就是"宁可慢不可漏"那一句：找不到同名测试不等于没有测试。"""
    gate = _load_gate()
    picked, why = gate.select_affected(["src/rolecard_agent/core/没有同名测试的模块.py"], _TESTS)
    assert picked == []
    assert "零命中" in why, why


def test_动了_src_与_tests_之外的文件当场退回全量() -> None:
    """pyproject / conftest / CI 工作流能影响整套用例的收集与运行方式。"""
    gate = _load_gate()
    picked, why = gate.select_affected(["src/rolecard_agent/rag/ocr.py", "pyproject.toml"], _TESTS)
    assert picked == []
    assert "横切面" in why, why


def test_没有_src_改动就不挑() -> None:
    """受影响选择只在改了 src/ 时生效 —— 其余情况保持"跑全套"这个原状，不自己发明政策。"""
    gate = _load_gate()
    picked, why = gate.select_affected(["README.md", "docs/代码审查快照.md"], _TESTS)
    assert picked == []
    assert "没有 src/" in why, why


def test_挑得太多也退回全量() -> None:
    """省不下多少就不省：一次"我到底跑全了没有"的疑问比那几秒贵。"""
    gate = _load_gate()
    tests = [f"tests/unit/test_x{i}.py" for i in range(5)]
    picked, why = gate.select_affected(["src/rolecard_agent/core/x.py"], tests)
    assert picked == []
    assert "省不下" in why, why


def test_改到的测试文件本身一定进清单() -> None:
    """按词干匹配未必匹配得到它自己（改了 `test_ocr_bundled_worker.py` 而没改任何 `ocr*` 模块）。

    这一格用 14 个文件的假套件：4 个的假套件上，"always-run + 同名 + 改到的测试"三个文件
    就超过六成，会被"挑得太多"那条先拦下来 —— 第一版用例就是那么红的（那条护栏本身是对的）。
    """
    gate = _load_gate()
    many = _TESTS + [f"tests/unit/test_filler{i}.py" for i in range(10)]
    picked, _ = gate.select_affected(
        ["src/rolecard_agent/rag/ocr.py", "tests/unit/test_别的东西.py"], many
    )
    assert "tests/unit/test_别的东西.py" in picked
    assert "tests/unit/test_ocr.py" in picked


def test_机器自己写的读数文件不算改动() -> None:
    """门禁每跑完一步就往 `docs/gate-readings.json` 写读数 —— 它必然出现在 git 清单里。

    而它按规矩属于"`src/` 与 `tests/` 之外"⇒ 一律退回全量。**第一趟真跑演示就是被它挡住的**
    （工作树里只有"一处 src 改动 + 这个文件"）：纯函数用例测的是"给一份改动清单挑得对不对"，
    量不到"清单本身从哪来"，所以这一格必须单独钉 —— 没有它，这条特性永远不会生效。
    """
    gate = _load_gate()
    picked, why = gate.select_affected(
        ["src/rolecard_agent/rag/ocr.py", "docs/gate-readings.json"], _TESTS
    )
    assert picked, why
    assert "tests/unit/test_ocr.py" in picked


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_门禁拼出来的命令行形状() -> None:
    """纯函数挑对了文件，还得**拼对命令行** —— 第一版真跑一趟才发现这一格。

    那时的症状：`--affected-subset` 是这一步自己的开关，却被当成文件清单的第一项转给
    pytest，于是 `unrecognized arguments: --affected-subset`（而且记号里报的文件数还多 1）。
    """
    gate = _load_gate()
    base = ["py", "scripts/pytest_with_evidence.py", "--lane", "fast"]
    cmd, note = gate._affected_pytest_command(  # noqa: SLF001
        base, ["src/rolecard_agent/rag/ocr.py", "docs/gate-readings.json"]
    )
    assert cmd[: len(base)] == base
    assert cmd[len(base)] == "--affected-subset", "开关必须紧跟 base，且只出现一次"
    assert cmd.count("--affected-subset") == 1
    picked = cmd[len(base) + 1 :]
    assert picked and all(p.startswith("tests/") and (ROOT / p).exists() for p in picked)
    assert "受影响子集" in note


def test_子集开关不许转给_pytest() -> None:
    """`pytest_with_evidence.py` 必须把 `--lane` 与 `--affected-subset` 一起摘掉。"""
    pwe = _load_script("pytest_with_evidence.py")
    assert pwe._SUBSET_FLAG == "--affected-subset"  # noqa: SLF001
    extra = pwe._extra_from_argv(  # noqa: SLF001
        ["--lane", "fast", "--affected-subset", "tests/unit/test_ocr.py", "tests/unit/test_api.py"]
    )
    assert extra == ["tests/unit/test_ocr.py", "tests/unit/test_api.py"], extra
    assert pwe._extra_from_argv(["--lane=fast"]) == []  # noqa: SLF001
    assert pwe._extra_from_argv(["--lane", "coverage"]) == []  # noqa: SLF001


def test_真工作树上挑出来的文件必须真的存在() -> None:
    """映射不许指向空气：真仓库上跑一次，挑中的每一个路径都要在磁盘上。

    （这条同时也是"它在真环境里跑得动"的检查：假清单挑得再准，`_changed_paths` 读错
    git 输出照样白搭 —— 而那正是"探测失误"最容易发生的地方。）
    """
    gate = _load_gate()
    changed = gate._changed_paths()  # noqa: SLF001 - 判据本身要问这一格
    picked, why = gate.select_affected(changed, gate._test_files())  # noqa: SLF001
    assert isinstance(why, str) and why
    for path in picked:
        assert (ROOT / path).exists(), f"{path} 被挑中了却不在磁盘上"
    assert set(picked) <= set(gate._test_files())  # noqa: SLF001
