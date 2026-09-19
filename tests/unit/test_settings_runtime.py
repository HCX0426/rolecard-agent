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


def test_siliconflow_credentials_are_visible_but_masked_and_readonly() -> None:
    """P1-4：云端嵌入/重排的凭据在界面上**看得见**（掩码）、但**不假装可改**。

    以前这把 key 只在 `rag/retriever` 里 `os.environ.get`，界面上一个字都没有 —— 用户无从
    知道检索到底用了谁的凭据。现在它进了配置契约，于是：
      * 必须掩码（`_SECRET_FIELDS` 漏一个就会把明文发给前端）；
      * 必须是只读（`kind="ro"`）：它的操作员时刻家是「模型」页，运行环境页给一个改了不
        生效的输入框，正是 P1-5 那一类假接缝。
    """
    settings = Settings(siliconflow_api_key="sk-1234567890abcd")
    payload = runtime_payload(settings)
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    rows = {r["key"]: r for r in groups["rag"]["items"]}  # type: ignore[index]

    key = rows["SILICONFLOW_API_KEY"]
    assert "sk-1234567890abcd" not in str(key["value"])
    assert str(key["value"]).endswith("abcd")
    assert key["kind"] == "ro"
    assert rows["SILICONFLOW_BASE_URL"]["value"] == "https://api.siliconflow.cn/v1"
    assert rows["SILICONFLOW_BASE_URL"]["kind"] == "ro"


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


def test_file_watch_row_editable_in_reachout_group() -> None:
    """文件事件触发（架构计划 C·§5.2）进「运行环境」主动开口组，且是可热切 bool。"""
    payload = runtime_payload(Settings())
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    rows = {r["key"]: r for r in groups["reachout"]["items"]}  # type: ignore[index]
    assert rows["FILE_WATCH_ENABLED"]["kind"] == "bool"
    assert rows["FILE_WATCH_ENABLED"]["field"] == "file_watch_enabled"
    assert rows["FILE_WATCH_ENABLED"]["default"] == rows["FILE_WATCH_ENABLED"]["value"]
