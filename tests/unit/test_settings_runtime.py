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


def test_backend_selection_is_not_a_runtime_knob() -> None:
    """P1-5：「用哪个后端」不在运行环境页 —— 那一页不再宣传任何改了不生效的开关。

    曾经 rag/ocr 组里挂着 嵌入后端 / 重排 / OCR 后端 / 云端 OCR Key / 云端 OCR 端点 五行
    "可改"，而工厂里对应的 env 分支在生产上从不执行（`seed_once` 恒播种一条启用的内置行）
    —— 保存它们等于什么都不发生。事实面在「服务」页的端点序 + 「模型」页的凭据。
    """
    payload = runtime_payload(Settings())
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    rows = {r["key"]: r for g in payload["groups"] for r in g["items"]}  # type: ignore[index]
    assert not {
        "RAG_EMBEDDING",
        "RAG_RERANK",
        "OCR_BACKEND",
        "OCR_API_KEY",
        "OCR_API_URL",
    } & set(rows)

    # 仍在的两项都必须是只读：解释器路径与云端兜底端点随进程构建，改了要重启。
    assert rows["OCR_PYTHON"]["kind"] == "ro"
    assert rows["SILICONFLOW_BASE_URL"]["kind"] == "ro"
    assert rows["SILICONFLOW_BASE_URL"]["value"] == "https://api.siliconflow.cn/v1"
    assert {"ocr", "rag", "web"} <= set(groups)


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
