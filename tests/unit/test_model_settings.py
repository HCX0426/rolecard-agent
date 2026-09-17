"""模型后端配置服务（core/model_settings.py）的单元测试。

覆盖三处代码审查修复的不变量：
  * H5：`effective_settings` 不再把「UI 已删、但 env 仍提供」的后端复活。
  * M2：对话默认后端必须落在 chat 用途行上（embedding/rerank/ocr 不能当默认）。
  * M8：`save` / `validate_base_url` 拒绝异常 scheme / 裸 host（SSRF 入口封堵）。
"""

from __future__ import annotations

import pytest

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.model_settings import (
    ModelSettingsError,
    ModelSettingsService,
    validate_base_url,
)
from rolecard_agent.storage.db import bootstrap, connect


def _conn() -> object:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    return c


# --------------------------------------------------------------------------- #
# H5: effective_settings 不复活被删的 env 后端
# --------------------------------------------------------------------------- #

def test_effective_settings_falls_back_to_env_before_seed() -> None:
    env = Settings(
        model_backends={
            "local": ModelBackend(
                provider="ollama", model="qwen2.5:7b",
                base_url="http://localhost:11434", api_key="ollama",
            ),
        },
        model_default="local",
        model_fallbacks=[],
    )
    svc = ModelSettingsService(_conn())
    # 表空（首启前）→ 直接退回 env 对象，不做任何合并。
    eff = svc.effective_settings(env)
    assert eff is env


def test_effective_settings_does_not_revive_deleted_env_backend() -> None:
    env = Settings(
        model_backends={
            "local": ModelBackend(
                provider="ollama", model="qwen2.5:7b",
                base_url="http://localhost:11434", api_key="ollama",
            ),
            # env 仍提供 ghost —— 但操作员已在 UI 删掉它。
            "ghost": ModelBackend(
                provider="openai", model="gpt-4o",
                base_url="https://api.openai.com/v1", api_key="sk-x",
            ),
        },
        model_default="local",
        model_fallbacks=[],
    )
    svc = ModelSettingsService(_conn())
    svc.seed_from_env(env)  # 首启：env 后端进表
    # 操作员删除 ghost（重存为只剩 local）。
    svc.save(
        default="local",
        backends=[{
            "name": "local", "provider": "ollama", "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434", "api_key": "ollama",
            "usage": "chat",
        }],
    )
    eff = svc.effective_settings(env)
    # 关键：env 里还有 ghost，但表已删 → 绝不复活。
    assert "ghost" not in eff.model_backends
    assert set(eff.model_backends) == {"local"}
    assert eff.model_default == "local"


# --------------------------------------------------------------------------- #
# M2: 对话默认后端必须落在 chat 用途行
# --------------------------------------------------------------------------- #

def test_save_rejects_non_chat_default() -> None:
    svc = ModelSettingsService(_conn())
    with pytest.raises(ModelSettingsError, match="必须是 chat 用途"):
        svc.save(
            default="emb",
            backends=[
                {"name": "emb", "provider": "ollama", "model": "bge-m3",
                 "usage": "embedding"},
                {"name": "chat", "provider": "ollama", "model": "qwen2.5:7b",
                 "usage": "chat"},
            ],
        )


def test_save_default_rejects_non_chat_backend() -> None:
    svc = ModelSettingsService(_conn())
    svc.save(
        default="chat",
        backends=[
            {"name": "emb", "provider": "ollama", "model": "bge-m3",
             "usage": "embedding"},
            {"name": "chat", "provider": "ollama", "model": "qwen2.5:7b",
             "usage": "chat"},
        ],
    )
    with pytest.raises(ModelSettingsError, match="chat 用途"):
        svc.save_default("emb", allowed_names={"emb", "chat"})


def test_save_prunes_stale_fallbacks_when_backends_shrink() -> None:
    """缩容保存（fallbacks 缺省 = 保留当前值）不得被陈旧链卡死（smoke 12/13 → 13/13）。

    旧链引用的 b 被删除后，"保留当前链"只能修剪为存活子集 —— 运行时
    `resolve_fallbacks` 本就丢弃未知名字，保存时对齐同一语义。
    """
    svc = ModelSettingsService(_conn())
    svc.save(
        default="a",
        backends=[
            {"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"},
            {"name": "b", "provider": "ollama", "model": "m-b", "usage": "chat"},
        ],
        fallbacks=["b"],
    )
    assert svc.list_fallbacks() == ["b"]
    svc.save(
        default="a",
        backends=[{"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"}],
    )  # 缩容：b 没了，链缺省保留 → 修剪后为空，保存成功
    assert svc.list_fallbacks() == []


def test_save_rejects_explicit_unknown_fallback() -> None:
    """显式给的链仍严格校验：指向不存在的后端要大声拒绝（手滑不能静默吞掉）。"""
    svc = ModelSettingsService(_conn())
    with pytest.raises(ModelSettingsError, match="不在已配置的后端列表里"):
        svc.save(
            default="a",
            backends=[{"name": "a", "provider": "ollama", "model": "m-a",
                       "usage": "chat"}],
            fallbacks=["ghost"],
        )


# --------------------------------------------------------------------------- #
# M8: base_url SSRF 校验
# --------------------------------------------------------------------------- #

def test_validate_base_url_rejects_bad_scheme() -> None:
    with pytest.raises(ModelSettingsError, match="必须包含协议"):
        validate_base_url("localhost:11434")
    with pytest.raises(ModelSettingsError, match="仅支持 http/https"):
        validate_base_url("file:///etc/passwd")
    with pytest.raises(ModelSettingsError, match="仅支持 http/https"):
        validate_base_url("gopher://evil")
    with pytest.raises(ModelSettingsError, match="缺少主机名"):
        validate_base_url("http://")


def test_validate_base_url_accepts_http_and_https_and_none() -> None:
    assert validate_base_url(None) is None
    assert validate_base_url("") is None
    assert validate_base_url("http://localhost:11434") == "http://localhost:11434"
    assert validate_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"


def test_save_rejects_bad_base_url() -> None:
    svc = ModelSettingsService(_conn())
    with pytest.raises(ModelSettingsError, match="仅支持 http/https"):
        svc.save(
            default="local",
            backends=[{
                "name": "local", "provider": "ollama", "model": "qwen2.5:7b",
                "base_url": "file:///etc/passwd", "usage": "chat",
            }],
        )
