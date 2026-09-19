"""运行时设置覆盖（core/runtime_settings.py，运行环境页签在线修改）的单元测试。

验证：覆盖写入/加载/类型解析、apply 优先级（DB 覆盖 env）、空值清除回落、
校验失败整体拒绝（半套配置比旧配置更危险）。
"""

from __future__ import annotations

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.core import runtime_settings as rs
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def conn():
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    return c


def test_save_load_roundtrip_with_types(conn) -> None:
    saved = rs.save_overrides(
        conn,
        {
            "web_allowed_domains": "example.com",
            "model_timeout_seconds": "30",
            "web_search_enabled": "0",
        },
    )
    assert set(saved) == {"web_allowed_domains", "model_timeout_seconds", "web_search_enabled"}
    o = rs.load_overrides(conn)
    assert o["web_allowed_domains"] == "example.com"
    assert o["model_timeout_seconds"] == 30.0
    assert o["web_search_enabled"] is False


def test_save_overrides_accepts_env_key(conn) -> None:
    """P0-1：前端按 payload.key（env 名）提交也要能落库，统一解析回字段名存储。"""
    saved = rs.save_overrides(conn, {"WEB_SEARCH_ENABLED": "0"})
    assert saved == ["web_search_enabled"]  # 落库以字段名为准
    assert rs.load_overrides(conn)["web_search_enabled"] is False
    # env 名与字段名混提，各自解析到对应字段
    rs.save_overrides(conn, {"CONTEXT_MAX_CHARS": "8000", "agent_default_mode": "agent"})
    o = rs.load_overrides(conn)
    assert o["context_max_chars"] == 8000 and o["agent_default_mode"] == "agent"


def test_apply_overrides_take_precedence_over_env(conn) -> None:
    base = Settings()  # env 默认：web_search_enabled=True
    assert base.web_search_enabled is True
    rs.save_overrides(conn, {"web_search_enabled": "0"})
    eff = rs.apply_overrides(base, rs.load_overrides(conn))
    assert eff.web_search_enabled is False
    # 无覆盖的基线：apply 是恒等
    assert rs.apply_overrides(base, {}) is base


def test_empty_value_clears_override(conn) -> None:
    rs.save_overrides(conn, {"web_allowed_domains": "example.com"})
    assert "web_allowed_domains" in rs.load_overrides(conn)
    rs.save_overrides(conn, {"web_allowed_domains": ""})  # 空串 = 清除覆盖，回落 env
    assert "web_allowed_domains" not in rs.load_overrides(conn)


def test_validation_rejects_all_or_nothing(conn) -> None:
    with pytest.raises(ValueError, match="不支持在线修改"):
        rs.save_overrides(conn, {"sqlite_path": "/x"})
    with pytest.raises(ValueError, match="只支持"):
        rs.save_overrides(conn, {"web_search_backend": "baidu"})
    with pytest.raises(ValueError, match="必须是数字"):
        rs.save_overrides(conn, {"model_timeout_seconds": "abc"})
    with pytest.raises(ValueError, match="只接受"):
        rs.save_overrides(conn, {"web_search_enabled": "maybe"})

    # 半套提交（合法+非法混合）整体拒绝：库里一个覆盖都不落
    with pytest.raises(ValueError):
        rs.save_overrides(conn, {"web_allowed_domains": "ok.com", "model_thinking": "maybe"})
    assert rs.load_overrides(conn) == {}
