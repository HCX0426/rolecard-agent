"""`scripts/baseline.py` 的"声明形状"必须真是建出来的（10-02 轮 `R102-03`）。

旧写法只 `connect()` 一个空库就去数 `sqlite_master`，而 `storage/db.py:connect()` 只设 PRAGMA、
不建表 —— 于是 `declared` 恒为空字典，`section_schema` 里那个 for 一次都不进，两根的
`column_drift_vs_declared` **永远写"无"**，接进门禁的第 12 步 `baseline --check`（R26-21 专门为
"算了没人看"接进来的那半步）从此**不可能红**。产物里的原件就是证据：`build/_baseline/declared-*.db`
实测 0 张表，而同一次跑出的 `routes-*.db` 是 24 张。

这三条用例钉的是：分母不是空的、分母等于 schema 原文声明的那些表、以及"空分母"这一格
本身会被那道守卫拦下来（不许回落成"无漂移"）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_baseline():
    spec = importlib.util.spec_from_file_location(
        "baseline_under_test", str(ROOT / "scripts" / "baseline.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_declared_shape_is_bootstrapped_not_an_empty_file():
    baseline = _load_baseline()
    shape = baseline.declared_shape()
    assert len(shape) >= 20, f"声明形状只建出 {len(shape)} 张表 —— 那是「没跑 bootstrap」的形状"
    assert "session_thread" in shape and "role_card" in shape


def test_declared_shape_covers_every_table_the_schema_files_declare():
    """分母与被补的那半必须同源：schema 原文声明的每张表都得真建出来。"""
    baseline = _load_baseline()
    declared = baseline.declared_table_names()
    built = set(baseline.declared_shape())
    missing = sorted(declared - built)
    assert not missing, f"schema 声明了却没建出的表：{missing}（漂移那格的分母被数脏了）"
    # 注释里那句 `CREATE TABLE IF NOT EXISTS`（core/schema.sql:295）不许被数成表名
    assert "IF" not in declared and "EXISTS" not in declared


def test_an_empty_declared_shape_raises_instead_of_passing(tmp_path, monkeypatch):
    """变异靶子：把建表那一步摘掉（回到旧行为），这条必须可乐而不是静默回空。"""
    baseline = _load_baseline()
    import rolecard_agent.storage.db as db

    monkeypatch.setattr(db, "bootstrap", lambda conn, enabled_domains=(): [])
    try:
        baseline.declared_shape()
    except RuntimeError as exc:
        assert "缺表" in str(exc), f"抛是抛了，但没说是为什么：{exc}"
    else:
        raise AssertionError("bootstrap 没建表时 declared_shape 居然安静返回了 —— 恒绿的旧病复发")


# ------------------------------------------------------------------ 列漂移的分面


def _fake_root(tmp_path: Path, ddl: str) -> Path:
    """造一份"实库"：只建一张 `t`，列形状由 ddl 给。"""
    import sqlite3

    db = tmp_path / "live.db"
    conn = sqlite3.connect(db)
    conn.execute(ddl)
    conn.commit()
    conn.close()
    return db


def test_drift_splits_hard_and_dormant_extra_columns(tmp_path, monkeypatch):
    """多出来的列按危害分面：NOT NULL 无默认的进 `drift`（拦），可空的进 `dormant`（放）。

    这条钉的是 baseline 判据从"extra 一律红"改成"分面"之后**没丢的那一半**：真会炸的
    列（新代码 INSERT 不写它，SQLite 当场拒绝）照旧红。把 `_columns` 的 notnull/默认
    判据摘成"全都算惰性"，这条第一处断言就红 —— 放行惰性列不等于把会炸的也放了。
    """
    baseline = _load_baseline()
    # t 表：a 声明里有；b NOT NULL 无默认（会炸）；c 可空（惰性死列）
    live = _fake_root(tmp_path, "CREATE TABLE t (a TEXT, b TEXT NOT NULL, c TEXT)")
    declared = {"t": {"a"}}
    monkeypatch.setattr(baseline, "data_roots", lambda: {"fake": live})

    section = baseline.section_schema(declared)
    entry = section["fake"]
    drift = entry["column_drift_vs_declared"]
    dormant = entry["dormant_extra_columns"]

    # 会炸的那列进 drift 的 extra
    assert drift == {"t": {"extra": ["b"], "absent": []}}, drift
    # 惰性那列进 dormant，不进 drift
    assert dormant == {"t": ["c"]}, dormant

    # `_drift_lines` 因此非空 —— 门禁照旧拦
    assert baseline._drift_lines({"schema_per_root": section}) != []


def test_dormant_only_extra_is_reported_not_blocked(tmp_path, monkeypatch):
    """只有惰性死列时：`--check` 放行（退出码 0），但那一格仍上屏 —— 放行≠看不见。

    这条钉的是分面的另一半，也是 R26-21 教训（"算了没人看等于没有"）在新判据下的
    续命：exemplars 那种退役通道的旧列不再拦升级，但必须被打印出来。把 dormant 那格
    从 `section_schema` 删掉，这条第二处断言红。
    """
    baseline = _load_baseline()
    live = _fake_root(tmp_path, "CREATE TABLE t (a TEXT, c TEXT DEFAULT '')")
    declared = {"t": {"a"}}
    monkeypatch.setattr(baseline, "data_roots", lambda: {"fake": live})

    section = baseline.section_schema(declared)
    entry = section["fake"]
    # 没有会炸的列 → drift 是"无" → _drift_lines 空 → --check 放行
    assert entry["column_drift_vs_declared"] == "无", entry["column_drift_vs_declared"]
    assert baseline._drift_lines({"schema_per_root": section}) == []
    # 但惰性列照旧记录在案（上屏靠它）
    assert entry["dormant_extra_columns"] == {"t": ["c"]}, entry["dormant_extra_columns"]
