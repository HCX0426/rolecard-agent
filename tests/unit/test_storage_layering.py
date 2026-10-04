"""`R102-08` + 2026-10-04 审查快照 P1-6：storage 不反向 import core，业务迁移由计划交进来。

三条判据：

  1. **方向不许反过来** —— storage 层任何一处 import `rolecard_agent.core`（含函数体里的
     惰性 import）即红；惰性 import 只是让导入不炸，环还在。
  2. **旧形态现场 + 没交计划 = 大声失败**，不是"静默不搬"：那一步的代价是用户配置变小
     （凭据组没建、chat 引用行没写），而症状长得像"我明明配过模型"。判据是**通用**的
     （声明 vs 实况，storage 里不点名任何表）—— 步骤清单住 `core/migrations.py`。
  3. **计划真的被执行**，并且是在 `bootstrap` 内部那个位置（顺序承重：搬层要排在
     service_endpoint 重建之后、`idx_model_backend_provider` 创建之前 —— 先建的索引会跟着
     DROP/RENAME 一起没了）。这条用"索引最后还在"来证。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rolecard_agent.core.migrations import build_plan
from rolecard_agent.storage.db import _columns, bootstrap, connect

REPO = Path(__file__).resolve().parents[2]
STORAGE = REPO / "src" / "rolecard_agent" / "storage"

#: 两层化之前的那张 `model_backend`（provider/base_url/api_key/usage 全在行里）。
LEGACY_MODEL_BACKEND = """
CREATE TABLE model_backend (
    name TEXT PRIMARY KEY,
    provider TEXT,
    base_url TEXT,
    model TEXT,
    api_key TEXT,
    usage TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    num_ctx INTEGER,
    supports_vision INTEGER,
    supports_tools INTEGER
)
"""


def test_storage_layer_never_imports_core() -> None:
    offenders: list[str] = []
    for path in sorted(STORAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "rolecard_agent.core"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("rolecard_agent.core"):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def _legacy_conn(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = connect(path)
    conn.execute(LEGACY_MODEL_BACKEND)
    conn.execute(
        "INSERT INTO model_backend (name, provider, base_url, model, api_key, usage)"
        " VALUES ('old_one', 'siliconflow', 'https://x/v1', 'm', 'sk-old', 'chat')"
    )
    conn.commit()
    return conn


def test_legacy_db_without_the_hook_fails_loud(tmp_path: Path) -> None:
    """没交 `plan` 的旧形态现场 = RuntimeError（消息点名 `MIGRATION_PLAN`）。"""
    conn = _legacy_conn(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="MIGRATION_PLAN"):
            bootstrap(conn, enabled_domains=())
    finally:
        conn.close()


def test_the_injected_action_runs_in_place(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_layers(conn) -> int:
        calls.append("moved")
        # 假装搬完：补上新形态那一列，让后面的索引语句有的可建。
        conn.execute("ALTER TABLE model_backend ADD COLUMN provider_id TEXT")
        return 1

    conn = _legacy_conn(tmp_path)
    try:
        bootstrap(
            conn, enabled_domains=("health",), plan=build_plan(provider_layers=fake_layers)
        )
        assert calls == ["moved"]
        # 顺序承重的证据：索引是在搬层**之后**建的，所以它此刻必须真的存在。
        indexes = {
            str(r["name"])
            for r in conn.execute("PRAGMA index_list('model_backend')").fetchall()
        }
        assert "idx_model_backend_provider" in indexes, indexes
        assert "provider_id" in _columns(conn, "model_backend")
    finally:
        conn.close()


def test_new_shape_db_needs_no_action(tmp_path: Path) -> None:
    """新库（有 provider_id）不该要求调用方传动作，也不该调用它。"""
    conn = connect(tmp_path / "fresh.db")
    calls: list[str] = []
    bootstrap(
        conn,
        enabled_domains=("health",),
        plan=build_plan(provider_layers=lambda _c: calls.append("x") or 0),
    )
    assert calls == []
    conn.close()
