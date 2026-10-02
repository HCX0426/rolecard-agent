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
