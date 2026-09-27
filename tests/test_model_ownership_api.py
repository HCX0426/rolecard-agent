"""模型凭据的归属，从**真输入链**进来验（M2d，§4.1「key 跟人走」）。

数据层的用例（`tests/unit/test_model_ownership.py`）钉的是服务层语义；这一支钉的是
"两个人分别从 HTTP 进来会看见什么"，因为这条链上有两处会骗人：

  * 请求身份 → `ctx.current_user()` → 服务层那个 `user_id` 参数（少接一次参数就串号）；
  * 整表 `PUT /api/settings/models` 的"全量替换"范围 —— 它的入参长得和从前一模一样，
    所以"替换的是这个人的全部"这件事只能靠端到端跑一次来证明。

四条各挡一个坏法：列表只回自己的组（掩码都不给对方）、A 的保存不抹 B 的行、
按名删别人的行是 404 而不是 204、探测别人的组是 400 而不是替他烧一次配额。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app

KEY_A = "sk-aaaaaaaaaaaaaaaaaa"
KEY_B = "sk-bbbbbbbbbbbbbbbbbbbb"


def _basic(user: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:pw".encode()).decode()


def _headers(user: str) -> dict[str, str]:
    return {"Authorization": _basic(user)}


def _cloud_body(name: str, model: str, key: str) -> dict[str, Any]:
    return {
        "default": name,
        "backends": [
            {
                "name": name,
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": model,
                "api_key": key,
                "usage": "chat",
            }
        ],
        "fallbacks": [],
    }


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """两个登录者：`local-user`（本机那份）与 `u1`（`app_user` 里补一行真身份）。"""
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", "u1:pw,local-user:pw")
    app = create_app(sqlite_path=tmp_path / "app.db")
    with TestClient(app) as test_client:
        conn = test_client.app.state.ctx.conn
        conn.execute(
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "VALUES ('u1', 'local', '第二个')",
        )
        conn.commit()
        yield test_client


def test_each_login_sees_only_its_own_credential_group(client: TestClient) -> None:
    assert client.put(
        "/api/settings/models", json=_cloud_body("a-chat", "Qwen3-8B", KEY_A),
        headers=_headers("local-user"),
    ).status_code == 200
    assert client.put(
        "/api/settings/models", json=_cloud_body("b-chat", "Qwen3-32B", KEY_B),
        headers=_headers("u1"),
    ).status_code == 200

    mine = client.get("/api/settings/models", headers=_headers("local-user")).json()
    theirs = client.get("/api/settings/models", headers=_headers("u1")).json()
    assert [g["id"] for g in mine["providers"]] != [g["id"] for g in theirs["providers"]]
    assert all(b["model"] == "Qwen3-8B" for g in mine["providers"] for b in g["models"])
    # 明文从来不上网络（拆层前就有的纪律），而**掩码也不能对得上**：两边同一把 key 的掩码
    # 相同，就等于告诉第二个身份"这里有人用过这把"，那是别人的凭据的存在性。
    assert KEY_A not in repr(mine) and KEY_B not in repr(mine)
    assert KEY_A not in repr(theirs) and KEY_B not in repr(theirs)
    masks_a = {str(g["key_masked"]) for g in mine["providers"]}
    masks_b = {str(g["key_masked"]) for g in theirs["providers"]}
    assert masks_a and masks_b and not (masks_a & masks_b), (masks_a, masks_b)


def test_whole_table_save_by_one_leaves_the_others_rows_alone(client: TestClient) -> None:
    """`PUT` 的"全量替换"替换的是**这个人**那一集（旧形状入参一字未改，所以更要端到端跑）。"""
    client.put("/api/settings/models", json=_cloud_body("b-chat", "Qwen3-32B", KEY_B),
               headers=_headers("u1"))
    again = _cloud_body("a-new", "Qwen3-14B", KEY_A)
    assert client.put("/api/settings/models", json=again,
                      headers=_headers("local-user")).status_code == 200
    left = client.get("/api/settings/models", headers=_headers("u1")).json()
    names = [b["model"] for g in left["providers"] for b in g["models"]]
    assert names == ["Qwen3-32B"], f"A 存了一次盘，B 的配置变小了：{names}"
    assert [g["has_key"] for g in left["providers"]] == [True]


def test_deleting_another_identitys_model_is_a_404(client: TestClient) -> None:
    client.put("/api/settings/models", json=_cloud_body("b-chat", "Qwen3-32B", KEY_B),
               headers=_headers("u1"))
    resp = client.delete("/api/settings/models/b-chat", headers=_headers("local-user"))
    assert resp.status_code == 404, resp.status_code
    assert client.get("/api/settings/models", headers=_headers("u1")).json()["providers"]


def test_probing_another_identitys_group_is_refused(client: TestClient) -> None:
    """探测 = 让服务器替他发一次真请求，所以要花的正是**他的**配额。"""
    body = _cloud_body("b-chat", "Qwen3-32B", KEY_B)
    client.put("/api/settings/models", json=body, headers=_headers("u1"))
    gid = client.get("/api/settings/models", headers=_headers("u1")).json()["providers"][0]["id"]
    resp = client.post(
        "/api/settings/models/probe",
        json={"provider_id": gid, "model": "Qwen3-32B"},
        headers=_headers("local-user"),
    )
    assert resp.status_code == 400, resp.status_code
    assert "不存在" in resp.json()["detail"]
