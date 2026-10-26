"""P14 文档命令块可执行化：探针的**锚点用例**（未完善清单 P14 的验收载体）。

README 已有探针护住（`probe_readme_quickstart.py`），其余文档的命令块仍靠人肉保鲜；
文档三族烂法里"命令会失效"是机器能接住的那一半。这条用例钉的是 `probe_docs_commands.py`
的三个契约面：

① **登记表完整**：首批登记的块（CONTRIBUTING §2 工作流、§8.2 主环境、§8.3 OCR、
   §8.4 国内镜像）都必须能从真文档里原样抽出来 —— 抽不到 = 文档结构改了而探针不知道。
② **形状臂**：登记块里引用的脚本路径 / 依赖清单文件必须真实存在 —— 故意改坏一条
   （P14 的验收原文）就在这里红。判据打在**替换出来的临时文档**上，不碰真文档。
③ **执行臂的取舍必须留痕**：不在执行臂的每一行都要有登记在案的**理由**
   （出网 / 会改真实环境 / 已有专属门禁步）——"静默跳过 = 生成物悄悄少一类"是本仓
   最忌讳的形状；执行的那两行（venv 建在临时目录、init_db 吃隔离数据根）必须真的
   隔离，探针绝不碰仓库根的真实状态。
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
_PROBE = ROOT / "scripts" / "probe_docs_commands.py"


def _load():
    spec = importlib.util.spec_from_file_location("pdc_under_test", str(_PROBE))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pdc_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_首批登记的四块都能从真文档里抽出来() -> None:
    pdc = _load()
    assert len(pdc.REGISTRY) == 4, "首批登记应当是 §2 + §8.2/8.3/8.4 四块"
    for entry in pdc.REGISTRY:
        lines = pdc._extract_block(ROOT / entry.doc, entry)
        assert lines, f"{entry.doc} / {entry.heading} 抽不出命令块 —— 文档结构漂了"
        assert any(not ln.startswith("#") for ln in lines), f"{entry.heading} 里全是注释"


def test_形状臂放行真文档() -> None:
    pdc = _load()
    problems: list[str] = []
    for entry in pdc.REGISTRY:
        lines = pdc._extract_block(ROOT / entry.doc, entry)
        problems.extend(pdc._shape_problems(entry, lines))
    assert not problems, f"真文档不该红：{problems}"


def test_故意改坏一条命令_形状臂当场红(tmp_path: pathlib.Path) -> None:
    """P14 验收原文：'故意改坏一条命令 → gate 红'。判据打在替身文档上。"""
    pdc = _load()
    real = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    broken = real.replace(
        "scripts\\init_db.py",
        "scripts\\init_database_renamed.py",
    )
    doc = tmp_path / "CONTRIBUTING.md"
    doc.write_text(broken, encoding="utf-8")
    problems: list[str] = []
    for entry in pdc.REGISTRY:
        lines = pdc._extract_block(doc, entry)
        problems.extend(pdc._shape_problems(entry, lines))
    assert problems, "改坏了脚本路径却没红 —— 形状臂是摆设"
    assert any("init_database_renamed" in p for p in problems), problems


def test_不执行的每一行都有登记在案的理由() -> None:
    """静默跳过 = 生成物悄悄少一类。pip / pytest 不真执行必须是**写明理由**的取舍。"""
    pdc = _load()
    for entry in pdc.REGISTRY:
        lines = pdc._extract_block(ROOT / entry.doc, entry)
        for line in lines:
            if line.lstrip().startswith("#") or not line.strip():
                continue
            decision = pdc._decide(entry, line)
            if decision.mode == "shape":
                assert decision.reason, f"没有理由的跳过：{line!r}"


def test_执行臂的三行都带隔离() -> None:
    pdc = _load()
    run_lines = [
        (entry, line)
        for entry in pdc.REGISTRY
        for line in pdc._extract_block(ROOT / entry.doc, entry)
        if pdc._decide(entry, line).mode == "run"
    ]
    assert len(run_lines) == 3, f"首批执行臂应当恰好三行（两处 venv + init_db）：{run_lines}"
    for _entry, line in run_lines:
        plan = pdc._plan_run(line, workdir=pathlib.Path("TBD"))
        assert plan.env, "执行没有环境隔离 —— 会写到仓库根的真实数据根"
        # venv 行的隔离在落点（cmd 里有临时目录），init_db 行的隔离在数据根（env 里）。
        assert "TBD" in plan.cmd or any("TBD" in v for v in plan.env.values()), (
            f"执行计划没把工作目录隔离进去：{plan.cmd!r} / {plan.env}"
        )


def test_端到端探针绿() -> None:
    """探针本体在真文档上跑一遍（venv 建临时目录 + init_db 吃隔离数据根，共 ~10s）。"""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(_PROBE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=300,
    )
    assert proc.returncode == 0, f"探针红了：\n{proc.stdout[-800:]}\n{proc.stderr[-400:]}"
    assert "✅" in proc.stdout, "绿要打在脸上（没有任何一行 ✅ = 判据没跑）"
