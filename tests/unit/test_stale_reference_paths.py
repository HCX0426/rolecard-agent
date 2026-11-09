"""A3（2026-11-03 架构审查）：导航文本里的**死路径**必须与磁盘上的真实落点一致。

判"死"只有一条标准：**今天按这个说法去找文件/函数，找不到**。这一类与"历史叙述"是两
回事 —— `R102-59` 从 `core/reachout.py` 拆出（当时它确实在 core，后来才迁 features）是
**记录**，把它改掉反而把历史改错了；而"老行的 uid 由 storage/db.py 里那段 `_migrate`
就地补"是**指令**（P1-6 那一步已经删了），照着去找的人会扑空 —— 后者才是这条尺子的靶子。

为什么必须有尺子：那些文本散在 `.sql` 注释、代码 docstring、`docs/`、`CONTRIBUTING.md`
四个面上，没有任何东西在量"它指的东西还在不在"。审查那一轮量出过 21 处
`core/migrations.py`，而那个文件从来不存在（P1-6 拆包后住 `core/storage/migrations.py`）。
本文件把审查发现的死路径固化成 denylist：**改名先改这里**（新落点写在报错信息里）。
"""

from __future__ import annotations

from pathlib import Path

#: 死路径 → 真实落点（红起来时，消息直接带着"该改成什么"）。
#:
#: 判死只看磁盘/函数是否还在，不猜语境；属性式点号引用（`core.memory.restore_row`）
#: **刻意不在**名单里 —— 那种写法按 import 解析得通，是风格问题不是死链。
DEAD_PATHS: dict[str, str] = {
    "core/identity": "base/identity（`DEFAULT_USER_ID` 的家）",
    "storage.db._migrate": "core/storage/migrations.py::_backfill_memory_item_uid",
    # 这一个**没有唯一后继**：那段 `_migrate` 是 P1-6 拆包时的整表重建/回填集合，拆进了
    # 各自的步骤。所以红起来的报错只说"按语义去点名步骤"，让人把落笔写实（本仓已修的
    # 几处：`_create_provider_index` / `_backfill_service_endpoint_owner` /
    # `migrate_to_provider_layers` / `rename_thread_id` …）。
    "storage/db.py::_migrate": "core/storage/migrations.py 里的对应步骤（按语义点名）",
    "core/migrations.py": "core/storage/migrations.py",
    "core/model_settings.py": "core/model_settings/（包；搬层在 migration.py）",
    "model_settings.py::": "core/model_settings/migration.py::",
}

#: 导航文本住的几个面：报错与注释（.py）、SQL 注释（.sql）、文档（.md）。
_SCAN_ROOTS = (
    Path("src"),
    Path("tests"),
    Path("scripts"),
    Path("docs"),
    Path("CONTRIBUTING.md"),
    Path("README.md"),
)
_EXTS = {".py", ".sql", ".md"}

#: 例外只有这一种形制：**这一行在声明那个路径不存在**（尺子自己解释靶子时会引用死路径
#: 原文 —— `scripts/consistency/checks_runtime.py` 的那条 docstring 就写着"那个文件从来
#: 不存在"）。引用它来指路 ≠ 指着它指路；按行豁免、不按文件豁免，免得整份文件失明。
_DECLARES_ABSENCE = "不存在"


def test_navigation_text_points_at_live_paths() -> None:
    """每一条死路径在仓里出现的位置 = 一条红（红先于改：这条应该先量出审查那批死引用）。"""
    offenders: list[str] = []
    files = 0
    for root in _SCAN_ROOTS:
        if root.is_file():
            candidates = [root]
        else:
            candidates = sorted(
                p
                for p in root.rglob("*")
                if p.is_file() and p.suffix in _EXTS and "__pycache__" not in p.parts
            )
        for path in candidates:
            files += 1
            rel = path.as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _DECLARES_ABSENCE in line:
                    continue
                for dead, real in DEAD_PATHS.items():
                    # 第二种形制：**denylist 条目自己**（`"<死路径>": "真实落点"`）。声明
                    # 什么是死链，本来就得写出死链 —— 这种行是尺子的表体，不是指路文本。
                    if line.lstrip().startswith(f'"{dead}"'):
                        continue
                    if dead in line:
                        offenders.append(f"{rel}:{lineno} `{dead}` → 应为 `{real}`")

    # 读法塌了也不许"扫到 0 条"当通过（本仓读法的通用纪律）：至少该扫到几份文件。
    assert files >= 80, f"只读到 {files} 份文件，这条尺子的扫描面塌了"
    assert not offenders, "这些导航文本指着不存在的落点（照着找会扑空）:\n  " + "\n  ".join(
        offenders
    )
