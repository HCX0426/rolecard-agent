"""运行环境只读视图（GET /api/settings/runtime）的单元测试。

验证三点：密钥只出掩码不出明文；分组齐全；与默认不同的行有 changed 标记。
"""

from __future__ import annotations

from rolecard_agent.api.routers.settings import runtime_payload
from rolecard_agent.config import Settings


def test_payload_groups_and_masks() -> None:
    settings = Settings(tavily_api_key="tvly-1234567890abcd", web_search_backend="tavily")
    payload = runtime_payload(settings)

    assert payload["groups"]
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    assert {"web", "ocr", "rag", "limit", "auth", "obs"} <= set(groups)

    web_rows = {r["key"]: r for r in groups["web"]["items"]}  # type: ignore[index]
    # 密钥只回掩码：末 4 位可见，明文绝不出现
    tavily = web_rows["TAVILY_API_KEY"]
    assert "tvly-1234567890abcd" not in str(tavily["value"])
    assert str(tavily["value"]).endswith("abcd")
    # changed 标记：值与默认不同 → True
    assert tavily["changed"] is True
    assert web_rows["WEB_SEARCH_BACKEND"]["changed"] is True

    # 默认值未改的行：changed=False
    obs_rows = {r["key"]: r for r in groups["obs"]["items"]}  # type: ignore[index]
    assert obs_rows["OBS_BACKEND"]["changed"] is False
    assert obs_rows["OBS_BACKEND"]["value"] == "local"


def test_secret_defaults_masked_even_when_unset() -> None:
    settings = Settings()
    payload = runtime_payload(settings)
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    auth_rows = {r["key"]: r for r in groups["auth"]["items"]}  # type: ignore[index]
    assert auth_rows["AUTH_CREDENTIALS"]["value"] == "未设置"


def test_thinking_models_choices_come_from_model_names() -> None:
    """思考名单的选项 = 用户模型页配置的模型名（动态下拉/勾选源）。"""
    settings = Settings()
    payload = runtime_payload(settings, model_names=["qwen3-vl:8b", "qwen3:8b"])
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    think_rows = {r["key"]: r for r in groups["think"]["items"]}  # type: ignore[index]
    assert think_rows["MODEL_THINKING_MODELS"]["choices"] == ["qwen3-vl:8b", "qwen3:8b"]
    # 枚举项 choices 照旧来自静态规格
    web_rows = {r["key"]: r for r in groups["web"]["items"]}  # type: ignore[index]
    assert web_rows["WEB_SEARCH_BACKEND"]["choices"] == ["auto", "tavily", "ddgs", "off"]
