"""模型页删除的连带清扫（运行环境覆盖）+ 审计历史保留在线化（用户 2026-11-09 五连问）。

钉三组行为：

1. **运行环境覆盖随模型页删除联动**（不是写死）：`MEMORY_EXTRACT_BACKEND` 存的是后端名、
   `MODEL_THINKING_MODELS` 存的是模型名 —— 模型页把那个模型删了，指向它的覆盖值不许悬空
   （悬空的形状最坏：运行环境页显示"记忆用 gone"，实际每一步都静默回落默认模型，
   症状正好是注释里警告过的"记忆悄悄提不出来"）。删除的两条路（DELETE 单行 / PUT 缩表）
   与服务引用行同事务清掉。
2. **审计历史保留可以在界面上设**：三个保留字段（天数 / 行数 / 审批终态天数）进运行环境
   注册表，保存**当场**执行清理（从前只有开机跑一次，改了要等重启 —— 而用户的诉求是
   "超过多少天自动删旧审计"，等重启等于没做）。0 = 该档永不清理。
3. **内置工具目录不再有英文说明**：`list_domains` / `list_roles` 的 docstring 首行进
   `/api/tools/catalog` 的 description —— 用户看得见的地方一律中文（用户 2026-11-09）。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app

MODELS = {
    "default": "local",
    "backends": [
        {
            "name": "local",
            "provider": "ollama",
            "model": "qwen3-vl:8b",
            "base_url": "http://127.0.0.1:9",
            "api_key": "x",
        },
        {
            "name": "gone",
            "provider": "siliconflow",
            "model": "BAAI/bge-m3",
            "api_key": "sk-c",
        },
    ],
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreachable_backend: str) -> TestClient:
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen3-vl:8b", "provider": "ollama",'
        f' "base_url": "{unreachable_backend}"}}}}',
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    return TestClient(create_app(sqlite_path=tmp_path / "hygiene.db"))


def runtime_row(client: TestClient, env_key: str) -> dict[str, object]:
    payload = client.get("/api/settings/runtime").json()
    return next(i for g in payload["groups"] for i in g["items"] if i["key"] == env_key)


# -- 1. 覆盖值随模型删除联动 -----------------------------------------------------------


def _seed_dangling_prone_overrides(client: TestClient) -> None:
    res = client.put(
        "/api/settings/runtime",
        json={
            "values": {
                "memory_extract_backend": "gone",
                # 名单里一个是被删后端的模型名，一个是留下来的：删后只该剩后者
                "model_thinking_models": "BAAI/bge-m3,qwen3-vl:8b",
            }
        },
    )
    assert res.status_code == 200, res.text
    assert runtime_row(client, "MEMORY_EXTRACT_BACKEND")["value"] == "gone"
    assert runtime_row(client, "MODEL_THINKING_MODELS")["override_value"] == (
        "BAAI/bge-m3,qwen3-vl:8b"
    )


def test_deleting_a_model_clears_runtime_overrides_that_point_at_it(
    client: TestClient,
) -> None:
    """DELETE 单行：记忆用谁 / 思考名单里指向被删模型的成分同事务清掉。"""
    assert client.put("/api/settings/models", json=MODELS).status_code == 200
    _seed_dangling_prone_overrides(client)
    assert client.delete("/api/settings/models/gone").status_code == 204

    row = runtime_row(client, "MEMORY_EXTRACT_BACKEND")
    assert row["value"] == "未设置", "覆盖值悬空在已删后端上：记忆提取会静默回落默认模型"
    assert row["overridden"] is False
    think = runtime_row(client, "MODEL_THINKING_MODELS")
    assert think["override_value"] == "qwen3-vl:8b"  # 只剔掉被删的那半，名单不整条蒸发


def test_shrinking_backends_also_clears_those_overrides(client: TestClient) -> None:
    """PUT 缩表与 DELETE 同结局 —— 两条删除路径不许两种行为（与服务引用行同一口径）。"""
    assert client.put("/api/settings/models", json=MODELS).status_code == 200
    _seed_dangling_prone_overrides(client)
    shrunk = {"default": "local", "backends": [MODELS["backends"][0]]}  # type: ignore[index]
    assert client.put("/api/settings/models", json=shrunk).status_code == 200
    assert runtime_row(client, "MEMORY_EXTRACT_BACKEND")["value"] == "未设置"
    assert runtime_row(client, "MODEL_THINKING_MODELS")["override_value"] == "qwen3-vl:8b"


def test_overrides_pointing_at_survivors_are_untouched(client: TestClient) -> None:
    """清扫只动悬空的：指向仍在的模型的覆盖必须一字不改地留着（这不是"删配置顺手清空页"）。"""
    assert client.put("/api/settings/models", json=MODELS).status_code == 200
    assert (
        client.put(
            "/api/settings/runtime",
            json={
                "values": {
                    "memory_extract_backend": "local",
                    "model_thinking_models": "qwen3-vl:8b",
                }
            },
        ).status_code
        == 200
    )
    assert client.delete("/api/settings/models/gone").status_code == 204
    assert runtime_row(client, "MEMORY_EXTRACT_BACKEND")["value"] == "local"
    assert runtime_row(client, "MODEL_THINKING_MODELS")["override_value"] == "qwen3-vl:8b"


# -- 2. 保留策略在线可设 + 保存即清 ----------------------------------------------------


def _insert_audit(db: Path, *, days_old: int, note: str) -> int:
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO audit_log (ts, actor, action, target, detail_json) "
            "VALUES (datetime('now', ?), 'user', 'tool.call', ?, ?)",
            (f"-{days_old} days", note, "{}"),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    finally:
        conn.close()


def _audit_targets(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [str(r[0]) for r in conn.execute("SELECT target FROM audit_log ORDER BY id")]
    finally:
        conn.close()


def test_retention_fields_are_editable_on_the_runtime_page(client: TestClient) -> None:
    """三个保留字段进注册表且可在线改（用户：「历史数据这里得可以设置」）。"""
    for key in ("AUDIT_LOG_RETENTION_DAYS", "AUDIT_LOG_MAX_ROWS", "APPROVAL_DONE_RETENTION_DAYS"):
        row = runtime_row(client, key)
        assert row["kind"] == "int", key
    bad = client.put("/api/settings/runtime", json={"values": {"audit_log_retention_days": "-1"}})
    assert bad.status_code == 400  # 负数大声拒绝；0 = 永不清理照旧收


def test_saving_retention_prunes_immediately_without_restart(
    client: TestClient, tmp_path: Path
) -> None:
    """保存即清（从前只在开机跑：改了天数要等重启才生效，等于"自动删旧审计"没上线）。"""
    db = tmp_path / "hygiene.db"
    old = _insert_audit(db, days_old=40, note="旧-该删")
    fresh = _insert_audit(db, days_old=1, note="新-该留")
    assert old < fresh  # id 单调，行数兜底判据测试依赖这个顺序
    assert (
        client.put(
            "/api/settings/runtime", json={"values": {"AUDIT_LOG_RETENTION_DAYS": "30"}}
        ).status_code
        == 200
    )
    targets = _audit_targets(db)
    assert "旧-该删" not in targets, "超龄行没被清：保存没当场执行清理"
    assert "新-该留" in targets
    # PUT 自己那条 update_runtime_settings 是刚写的（ts=now），30 天线碰不到它 ——
    # 清理不许反过来把"记录这次清理"的账删了，那是自噬。
    assert "runtime" in targets


def test_row_cap_prunes_keeping_the_newest(client: TestClient, tmp_path: Path) -> None:
    """行数档：超出的按最旧删，最新 N 条留住 —— 与天数档任一命中即清，两条互不越权。"""
    db = tmp_path / "hygiene.db"
    for i in range(3):
        _insert_audit(db, days_old=0, note=f"行{i}")
    # 5 档装得下"3 行存量 + PUT 自己那条"，一条都不该动
    assert (
        client.put(
            "/api/settings/runtime", json={"values": {"AUDIT_LOG_MAX_ROWS": "5"}}
        ).status_code
        == 200
    )
    assert _audit_targets(db) == ["行0", "行1", "行2", "runtime"]
    # 收到 1 档：只留最新那条（PUT 自己的记录），最旧的三条按序蒸发
    assert (
        client.put(
            "/api/settings/runtime", json={"values": {"AUDIT_LOG_MAX_ROWS": "1"}}
        ).status_code
        == 200
    )
    assert _audit_targets(db) == ["runtime"]


# -- 3. 工具目录的说明一律中文 ---------------------------------------------------------


def test_kernel_tool_descriptions_are_chinese(client: TestClient) -> None:
    """`/api/tools/catalog` 的 description 取工具 docstring 首行 —— 用户看得见的地方
    不许夹英文（用户 2026-11-09：「内置工具目录有些工具是英文注释，改成中文」）。"""
    kernel = client.get("/api/tools/catalog").json()["kernel"]
    assert kernel, "目录空了这条用例就失去意义"
    bad = [t["name"] for t in kernel if not re.search(r"[\u4e00-\u9fff]", str(t["description"]))]
    assert bad == [], f"这些内核工具的说明不是中文：{bad}"


def test_zero_means_never_prune(client: TestClient, tmp_path: Path) -> None:
    """0 = 该档永不清理（旧语义原样保留，界面说明就按这个写）。"""
    db = tmp_path / "hygiene.db"
    _insert_audit(db, days_old=400, note="很旧但被豁免")
    assert (
        client.put(
            "/api/settings/runtime",
            json={"values": {"AUDIT_LOG_RETENTION_DAYS": "0", "AUDIT_LOG_MAX_ROWS": "0"}},
        ).status_code
        == 200
    )
    # 很旧那行在豁免档下一根汗毛都不能动（PUT 自己那条新写的记录当然也在）
    assert "很旧但被豁免" in _audit_targets(db)
