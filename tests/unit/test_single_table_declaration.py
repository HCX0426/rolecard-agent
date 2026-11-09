"""A4（2026-11-03 架构审查）：**一张表只能有一处 CREATE** —— 声明面在 `schema.sql`。

迁移步骤里的 `CREATE TABLE IF NOT EXISTS` 一旦与声明面重名，就成了第二份事实源：谁看
都不会红（`IF NOT EXISTS` 静默跳过），但"库里这张表到底从哪来"从此没有答案。更实的一层
是这类函数**根本跑不到**：bootstrap 的顺序是 schema DDL 在前、shape 步骤在后，DDL 已经把
表建好，`_ensure_*` 那一族每次开机都走空路 —— 死代码却假装在守门（审查 A4 的原始发现）。

判据写成**交集**而不是点名删两个函数：迁移文件里允许出现的建表只有两种 ——
  * 临时暂存表（`…__b2` / `…_new`：整表重建的中转，名字刻意带后缀，不会进声明）；
  * 声明面没有的索引（`idx_model_backend_provider`：必须在搬层**之后**建，见那一步的
    docstring —— 这一条顺序承重，只能活在迁移里）。
名字一旦与 `schema.sql` / 各域声明相撞，就是第二份真相，判红。

读法两条（第一版各踩过一次，都记在函数里）：
  * `.py` 只读**代码里的字符串**（AST），docstring 与注释不看 —— 讲历史的散文里写着
    `CREATE TABLE IF NOT EXISTS` 是记录，不是声明；这一分法与
    `check_error_message_paths_resolve`"散文是记录、报错是指令"同一条口径；
  * `.sql` 先剥 `--` 注释行再读 —— 同一个理由（声明里有大量讲解性注释）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SRC = Path("src/rolecard_agent")
_MIGRATIONS = _SRC / "core/storage/migrations.py"
_CREATE = re.compile(
    r"CREATE\s+(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
#: 引用符先剥掉（`CREATE TABLE IF NOT EXISTS \`x\`` 这种形制），否则正则的可选组吃不住
#: 就会回溯成"没有那半段"、把 `IF` 当成名字 —— 第一版实测就是这个假名混进了红名单。
_QUOTE_CHARS = ("`", "[", "]", '"')


def _code_strings(path: Path) -> str:
    """`.py` 里**作为指令执行**的字符串常量，docstring 除外（散文是记录，不是声明）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            head = body[0]
            if (
                isinstance(head, ast.Expr)
                and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)
            ):
                docstring_ids.add(id(head.value))
    chunks: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            chunks.append(node.value)
    return "\n".join(chunks)


def _sql_text(path: Path) -> str:
    """`.sql` 的指令文本：`--` 注释剥掉（注释里讲解 `CREATE` 的地方不少）。"""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cut = line.find("--")
        lines.append(line if cut < 0 else line[:cut])
    return "\n".join(lines)


def _create_names(text: str) -> set[str]:
    for char in _QUOTE_CHARS:
        text = text.replace(char, " ")
    return set(_CREATE.findall(text))


def _declared_names() -> set[str]:
    """声明面（所有 `*.sql` schema 文件，注释除外）里的表名与索引名。"""
    names: set[str] = set()
    for sql in sorted(_SRC.rglob("*.sql")):
        names.update(_create_names(_sql_text(sql)))
    return names


def test_migration_steps_never_redeclare_a_declared_table() -> None:
    """迁移步骤里出现的建表/建索引，一个都不许与 schema 声明同名（红先于删）。"""
    created = sorted(_create_names(_code_strings(_MIGRATIONS)))
    declared = _declared_names()
    offenders = [name for name in created if name in declared]

    # 读法自检：声明面必须真读到东西、迁移侧必须真读到 CREATE（扫到 0 条不许当通过）。
    assert len(declared) >= 30, f"只读到 {len(declared)} 个声明名，读法塌了"
    assert created, "迁移文件里一个 CREATE 都扫不到，这条尺子的读法坏了"
    assert not offenders, (
        "这些名字在 schema 声明里已经有一处 CREATE，迁移步骤不许再声明一次"
        "（第二份事实源；要建表就改 schema.sql）: "
        f"{offenders}"
    )
