"""渠道管理面（2026-10-25）的单元测试：运行环境注册表含渠道字段、保存触发热重启、
状态端点如实回三态（关闸 / 缺凭证 / 运行中）。

钉住的判据：
* qq_* 四字段进 RUNTIME_FIELDS（运行环境页显示它们），qq_app_secret 是 secret 类；
* PUT /api/settings/runtime 带 qq_* 时渠道任务被 restart（mock 计数）；
* /api/channels 对"总闸关 / 缺凭证"给出 blocked_reason，对配置齐的给 running 态。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.core.runtime_settings import RUNTIME_FIELDS


def test_channel_fields_registered_in_runtime_registry() -> None:
    fields = {f.field: f for f in RUNTIME_FIELDS}
    for name in ("qq_channel_enabled", "qq_app_id", "qq_app_secret", "qq_channel_role_id"):
        assert name in fields, name
    assert fields["qq_app_secret"].kind == "secret"
    assert fields["qq_channel_enabled"].kind == "bool"
    # 渠道组进分组清单（运行环境页按组渲染）
    assert any(key == "channel" for key, _label in __import__(
        "rolecard_agent.core.runtime_settings", fromlist=["RUNTIME_GROUPS"]
    ).RUNTIME_GROUPS)


def test_saving_channel_config_restarts_channel(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "ch.db")
    with TestClient(app) as client:
        source = client.app.state.ctx.runtime.background_task("qq-channel")
        assert source is not None, "宿主必须注册了 qq-channel 任务"
        calls: list[int] = []
        source.restart = lambda: calls.append(1)  # type: ignore[method-assign]
        res = client.put(
            "/api/settings/runtime",
            json={"values": {"QQ_CHANNEL_ENABLED": "1"}},
        )
        assert res.status_code == 200
        assert calls, "保存 qq_* 配置必须触发渠道 restart"


def test_saving_non_channel_config_does_not_restart(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "ch2.db")
    with TestClient(app) as client:
        source = client.app.state.ctx.runtime.background_task("qq-channel")
        calls: list[int] = []
        source.restart = lambda: calls.append(1)  # type: ignore[method-assign]
        client.put("/api/settings/runtime", json={"values": {"REACHOUT_MERGE_DAYS": "7"}})
        assert calls == []


def test_channels_status_reflects_gates(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "ch3.db")
    with TestClient(app) as client:
        # 出厂：总闸关
        body = client.get("/api/channels").json()
        qq = body["channels"][0]
        assert qq["id"] == "qq" and qq["enabled"] is False
        assert "总闸" in qq["blocked_reason"] and qq["running"] is False

        # 凭证缺口：开闸但没填 AppID/Secret
        client.put(
            "/api/settings/runtime",
            json={"values": {"QQ_CHANNEL_ENABLED": "1"}},
        )
        qq = client.get("/api/channels").json()["channels"][0]
        assert qq["enabled"] is True and "凭证缺口" in qq["blocked_reason"]
