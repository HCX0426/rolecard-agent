"""最底两层（`base` / `storage`）不许反向 import 上层 —— 这条边界不靠"约定"维持。

为什么单独立一个测试而不是只靠 import-linter 契约：契约在门禁的静态并发组里跑，
而有人单独 `pytest tests/unit/test_x.py` 时不会触发它。这一条用最便宜的 AST 扫，
在任意一次 pytest 里都把"底层反向 import 上层"钉成红。

它同时接管了一件从前单独守着的事：一致性脚本里那条 `storage does not import core`
已随本次依赖收口**删除** —— `pyproject` 的分层契约禁止 storage import 任何上层（不只是
core），实测连函数内的延迟 import 也照样照出来（那正是 `R102-08` 当年断开的那个形状）。
一把尺子管住整类，比两处各守一条便宜，也更难漏。

判据只问方向，不问内容：
  * `base` 允许 import —— 标准库 / 第三方 / `rolecard_agent.config` / `rolecard_agent.storage`
    / `base` 自己内部（`base` 在 `storage` 之上：identity 与 audit 都要拿 `SqlConnection`）。
  * `storage` 允许 import —— 标准库 / 第三方 / `rolecard_agent.config`。
  * 出现 `rolecard_agent.(core|rag|roles|domains|api|features)` ⇒ 红。
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "rolecard_agent"

#: 一旦 import 这些包，就说明有人在把上层的知识往底层抄近道。
UPWARD = frozenset({"core", "rag", "roles", "domains", "api", "features"})

#: 每个底层包各自还**允许**往哪几层依赖（含它自己的兄弟模块）。不在允许集里、
#: 又不在 UPWARD 里的，是"底层之间乱串"（例如 storage 反过来 import base ——
#: 那是 base 在 storage 之上，方向不对）。
ALLOWED_DOWNWARD = {
    "base": frozenset({"config", "storage", "base"}),
    "storage": frozenset({"config", "storage"}),
}


def _imported_packages(path: pathlib.Path) -> set[str]:
    """一个模块 import 到的 `rolecard_agent.<pkg>` 里的那些 `<pkg>`（含函数内的延迟 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        for name in names:
            prefix = "rolecard_agent."
            if name.startswith(prefix):
                found.add(name[len(prefix) :].split(".")[0])
    return found


def test_bottom_layers_never_import_upward() -> None:
    offenders: list[str] = []
    for pkg, allowed in ALLOWED_DOWNWARD.items():
        files = sorted(
            p for p in (SRC / pkg).rglob("*.py") if "__pycache__" not in p.parts
        )
        assert files, f"{pkg} 包空了？找 {SRC / pkg}"
        for path in files:
            imported = _imported_packages(path)
            bad = sorted((imported & UPWARD) | (imported - allowed - UPWARD))
            if bad:
                rel = path.relative_to(SRC).as_posix()
                offenders.append(f"{rel} -> {bad}")
    assert not offenders, (
        "底层反向 import 上层（依赖方向被抄回来了）：" + "; ".join(offenders)
    )


def test_base_is_where_the_shared_primitives_live() -> None:
    """下沉的那几件仍在原位（改名/挪走都会撞这条，逼着改的人先想清楚方向）。"""
    names = {p.stem for p in (SRC / "base").glob("*.py") if p.stem != "__init__"}
    assert {
        "text",
        "markers",
        "app_identity",
        "paths",
        "observability",
        "identity",
        "audit",
        "outbound",
        "probes",
        "scopes",
    } <= names
