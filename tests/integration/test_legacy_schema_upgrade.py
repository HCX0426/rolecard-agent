"""老库升得上来吗 —— 09-26 轮 `R26-04` 的门禁用例。

为什么钉在这儿：`_migrate` 原先那 12 处手写 ALTER 全在核心表上，域表零覆盖，而
`bootstrap` 先跑各 `schema.sql`（里面有 `CREATE INDEX ... (新列)`）再跑 `_migrate` ——
于是**任何一台拿着老形状的机器一启动就炸**。09-25 实测：14cb9db 那份形状抛
`sqlite3.OperationalError: no such column: ingestion_task_id`。

修法是"声明驱动补列"（`storage/db.py:reconcile_columns`），所以这里断言的不是
"某条 ALTER 在不在"，而是**升级之后的列集合与 `schema.sql` 的声明一致** ——
以后谁加了列忘了写迁移，红的是这一条，而不是用户机器上的第一次读。

`tests/fixtures/legacy_schemas/` 那六份 .sql 是 `git show <commit>:<path>` 原样取出的，
一个字符都没改；两个 commit 分别是本仓第一个 scaffold 提交与"ingestion_task 拆表"那一次。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rolecard_agent.core.model_settings import _tools_of, _vision_of
from rolecard_agent.domains.registry import DOMAINS
from rolecard_agent.storage.db import bootstrap, connect, reconcile_columns, schema_files

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_schemas"

#: commit → 那份形状当时有哪几个 schema 文件（`finance` 域当时还不存在）。
LEGACY_SHAPES = {
    "14cb9db": ("core", "roles", "domains_health"),
    "03631c6": ("core", "roles", "domains_health"),
}


def _declared() -> dict[str, set[str]]:
    """当前声明的形状：`{表: 列集合}`（在内存库里把现版 DDL 跑一遍读出来）。"""
    probe = sqlite3.connect(":memory:")
    probe.row_factory = sqlite3.Row
    for path in schema_files(DOMAINS):
        probe.executescript(path.read_text(encoding="utf-8"))
    return {
        str(t): {str(r["name"]) for r in probe.execute(f'PRAGMA table_info("{t}")')}
        for (t,) in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _build_legacy(commit: str, path: Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.row_factory = sqlite3.Row
    for part in LEGACY_SHAPES[commit]:
        conn.executescript((FIXTURES / f"{commit}_{part}.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f'PRAGMA table_info("{table}")')}


@pytest.mark.parametrize("commit", sorted(LEGACY_SHAPES))
def test_legacy_db_boots_without_error(commit: str, tmp_path: Path) -> None:
    """升级路径本身必须走得通 —— 这条就是 R26-04 那个崩溃的正面反证。"""
    conn = _build_legacy(commit, tmp_path / "app.db")
    missing_before = sorted(set(_declared()) - _tables(conn))
    assert missing_before, f"{commit} 那份形状不该已经是新库"

    applied = bootstrap(conn, enabled_domains=DOMAINS)

    assert applied, "一个 schema 文件都没应用 = 静默跳过了建表"
    conn.close()


@pytest.mark.parametrize("commit", sorted(LEGACY_SHAPES))
def test_upgraded_columns_match_declaration(commit: str, tmp_path: Path) -> None:
    """逐表断言"声明里有的列，升完都在"。漏一处 ALTER 从此在这里红。"""
    conn = _build_legacy(commit, tmp_path / "app.db")
    bootstrap(conn, enabled_domains=DOMAINS)

    declared = _declared()
    gaps = {
        table: sorted(declared[table] - _cols(conn, table))
        for table in declared
        if table in _tables(conn) and declared[table] - _cols(conn, table)
    }
    assert not gaps, f"{commit} 升完之后仍然缺：{gaps}"
    conn.close()


def test_upgrade_keeps_existing_rows_and_fills_defaults(tmp_path: Path) -> None:
    """升级不许动数据：老行原样在，新列按声明的默认值落地。

    这条是"通用补列器"唯一真正危险的地方 —— 补错了不能只报"列有了"，要报"人家的数据呢"。
    """
    conn = _build_legacy("14cb9db", tmp_path / "app.db")
    conn.execute(
        "INSERT INTO role_card (role_id, role_name, system_prompt)"
        " VALUES ('u', '旧卡', '旧设定')"
    )
    conn.commit()

    bootstrap(conn, enabled_domains=DOMAINS)

    row = conn.execute(
        "SELECT role_id, role_name, reachout_enabled, recall_enabled, file_watch_enabled"
        " FROM role_card WHERE role_id = 'u'"
    ).fetchone()
    assert row is not None, "升级把原有的角色卡弄丢了"
    assert (row["role_name"], row["reachout_enabled"], row["recall_enabled"]) == (
        "旧卡",
        0,
        1,
    ), "新列没按 schema.sql 声明的默认值落地"
    # 出厂静默这一条必须是 0：默认给成 1 就等于替用户打开了主动开口。
    assert conn.execute(
        "SELECT COUNT(*) FROM role_card WHERE reachout_enabled = 1"
    ).fetchone()[0] == 0
    conn.close()


def test_legacy_extra_columns_survive(tmp_path: Path) -> None:
    """声明里没有的旧列**不删**：那是"退成只读"还是"删掉"的决定，不是升级该顺手做的事。

    14cb9db 的 `medical_report` 带着 `source_file`/`status`/`file_hash` 三列，现版声明已经
    没有它们。补列器只管补缺，不管理赔 —— 删列要走 `_migrate` 里那种显式整表重建。
    """
    conn = _build_legacy("14cb9db", tmp_path / "app.db")
    before = _cols(conn, "medical_report")
    bootstrap(conn, enabled_domains=DOMAINS)
    assert before <= _cols(conn, "medical_report")
    conn.close()


def test_bootstrap_is_idempotent_on_a_legacy_db(tmp_path: Path) -> None:
    """同一个老库连升两次不许红（真实场景：进程重启每次都跑 bootstrap）。"""
    conn = _build_legacy("03631c6", tmp_path / "app.db")
    bootstrap(conn, enabled_domains=DOMAINS)
    first = {t: _cols(conn, t) for t in _tables(conn)}
    bootstrap(conn, enabled_domains=DOMAINS)
    assert {t: _cols(conn, t) for t in _tables(conn)} == first
    conn.close()


def test_reconcile_adds_a_column_that_only_the_declaration_has(tmp_path: Path) -> None:
    """机制本身的可证伪面：造一个"声明里多一列"的场景，确认它被自动补上。

    这就是"以后加一列忘了写迁移"的最小复现 —— 修完之后它不该再炸，而该静默补齐。
    """
    db = tmp_path / "app.db"
    fresh = connect(db)
    bootstrap(fresh, enabled_domains=DOMAINS)
    fresh.execute("INSERT INTO role_card (role_id, role_name, system_prompt, description)"
                  " VALUES ('r', 'n', 'p', '这段说明')")
    fresh.commit()
    fresh.close()

    conn = connect(db)
    conn.execute("ALTER TABLE role_card DROP COLUMN description")
    conn.commit()
    assert "description" not in _cols(conn, "role_card")

    added = reconcile_columns(conn, files=schema_files(DOMAINS))

    assert "role_card.description" in added
    assert "description" in _cols(conn, "role_card")
    conn.close()


def test_shape_migrated_tables_are_left_to_migrate(tmp_path: Path) -> None:
    """`model_backend` 的旧形态**不能**被补列器提前补出 `provider_id`。

    `_migrate` 靠"有没有 provider_id"判形态并搬层；提前补上它 = 搬层被跳过 = 旧行的凭据
    静静留在没人再读的列里。这一条钉的就是那个跳过。
    """
    db = tmp_path / "app.db"
    conn = connect(db)
    bootstrap(conn, enabled_domains=DOMAINS)
    conn.execute("DROP TABLE model_backend")
    conn.execute(
        "CREATE TABLE model_backend ("
        " name TEXT PRIMARY KEY, provider TEXT, base_url TEXT, api_key TEXT,"
        " model TEXT, usage TEXT NOT NULL DEFAULT 'chat', sort_order INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO model_backend (name, provider, model) VALUES ('old','ollama','m')")
    conn.commit()

    added = reconcile_columns(conn, files=schema_files(DOMAINS))

    assert not [a for a in added if a.startswith("model_backend.")], (
        f"补列器把手形迁移的活抢了：{added}"
    )
    assert "provider_id" not in _cols(conn, "model_backend")
    conn.close()


def test_a_legacy_upgrade_leaves_the_capability_flags_unmeasured(tmp_path: Path) -> None:
    """旧形态库升上来之后，`supports_vision` / `supports_tools` 必须是 **NULL（没测过）**。

    旧写法回填成 `NOT NULL DEFAULT 0/1`。运行时的数值一模一样（`_vision_of(NULL)=False`、
    `_tools_of(NULL)=True`），坏的是**声明**：模型页把那两格渲染成"✗ / ✓"，用户以为有人
    测过，其实一次都没测 —— 而"没测过"该显示成 `?`（`list_providers` 走 `_tri_state`）。
    本轮猜测区那条"升级回填与新建路径语义不一致"就是这么定成实的（新建路径写 NULL 是对的，
    错的是升级路径）。
    """
    db = tmp_path / "app.db"
    conn = connect(db)
    bootstrap(conn, enabled_domains=DOMAINS)
    conn.execute("DROP TABLE model_backend")
    conn.execute(
        "CREATE TABLE model_backend ("
        " name TEXT PRIMARY KEY, provider TEXT, base_url TEXT, api_key TEXT,"
        " model TEXT, usage TEXT NOT NULL DEFAULT 'chat', sort_order INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO model_backend (name, provider, model) VALUES ('old','ollama','m')")
    conn.commit()
    # 第二次启动才走得到形态判定与搬层（第一次建的是新库形状）
    bootstrap(conn, enabled_domains=DOMAINS)

    row = conn.execute(
        "SELECT supports_vision, supports_tools FROM model_backend WHERE name = 'old'"
    ).fetchone()
    assert row is not None, "搬层把这一行弄丢了"
    assert row[0] is None and row[1] is None, f"升级替用户回答了没人问过的问题：{tuple(row)}"
    # 运行时口径没变 —— 这句是"为什么这个修正零风险"的证据，不是附赠断言。
    # 直接引这两个私有函数是故意的：语义就长在它们身上，绕道公开接口反而断不到同一件事。
    assert _vision_of(row[0]) is False
    assert _tools_of(row[1]) is True
    conn.close()


@pytest.mark.parametrize(
    ("commit", "expected_missing"), [("14cb9db", 12), ("03631c6", 9)], ids=lambda v: str(v)
)
def test_audited_shortfall_is_still_the_shortfall(
    commit: str, expected_missing: int, tmp_path: Path
) -> None:
    """把"老库缺几列"这组数钉在测试上：它**本来就该随 schema 增长而漂**。

    审计当场（HEAD `e5de500`）量到的是 11 / 8，合计 19；同日补上 `affinity_enabled` 与
    `recall_at` 之后变成 12 / 9（`role_proactive_state` 在那两份形状里整表还不存在，
    所以 `recall_at` 不进这个数）。这条红的用途不是"证明坏了"，而是**逼改 schema 的人
    回去看一眼审计文档还写着几个数** —— R26-06 犯的就是"写进文档的数没复算"这一条。
    """
    conn = _build_legacy(commit, tmp_path / "app.db")
    declared = _declared()
    existing = _tables(conn)
    short = {t: declared[t] - _cols(conn, t) for t in declared if t in existing}
    total = sum(len(v) for v in short.values())
    assert total == expected_missing, f"{commit} 现在缺 {total} 列，审计写的是 {expected_missing}"
    # 一处"NOT NULL 且无默认"都没有 —— 那种 SQLite 拒绝 ADD COLUMN，得走整表重建。
    unaddable = [
        f"{t}.{c}"
        for t, cols in short.items()
        for c in cols
        if any(
            r["notnull"] and r["dflt_value"] is None
            for r in conn.execute(f'PRAGMA table_info("{t}")')
            if r["name"] == c
        )
    ]
    assert not unaddable, f"{commit} 出现了补列器补不了的形状：{unaddable}"
    conn.close()
