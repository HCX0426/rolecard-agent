"""运行环境只读视图（GET /api/settings/runtime）的单元测试。

验证五点：密钥只出掩码不出明文；分组齐全；与默认不同的行有 changed 标记；
**渲染与冻结金样逐字节一致**；**注册表新条目自动进视图**（单注册表的两把守卫）。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from rolecard_agent.api.routers.settings import runtime_payload
from rolecard_agent.config import ModelBackend, Settings


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


def test_memory_extract_backend_choices_are_backend_names() -> None:
    """「记忆后端」的下拉给的是**后端名**（`resolve_role_model` 认的那份），不是模型列。

    为什么单独钉这一条：填错了不报错 —— 它静默回落默认模型，症状正好是 §12.5 要修的
    "记忆悄悄提不出来"。而模型列可以重名（两个后端都跑 qwen3:8b），所以"models"那个
    选项源顶替不了它。
    """
    base = Settings()
    settings = base.model_copy(
        update={
            "model_backends": {
                "local": base.model_backends["local"],
                "云端强模型": ModelBackend(model="qwen3:8b", provider="openai"),
            }
        }
    )
    payload = runtime_payload(settings, model_names=["some-model"])
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    rag_rows = {r["key"]: r for r in groups["rag"]["items"]}  # type: ignore[index]
    row = rag_rows["MEMORY_EXTRACT_BACKEND"]
    assert row["choices"] == ["local", "云端强模型"]
    assert row["kind"] == "str"
    # 没填 = 出厂默认（空串在界面上显示成"未设置"，而它正是"跟随会话/角色"那一档）
    assert row["value"] == "未设置" and row["changed"] is False


def test_file_watch_row_editable_in_reachout_group() -> None:
    """文件事件触发（架构总览 §5）进「运行环境」主动开口组，且是可热切 bool。"""
    payload = runtime_payload(Settings())
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    rows = {r["key"]: r for r in groups["reachout"]["items"]}  # type: ignore[index]
    assert rows["FILE_WATCH_ENABLED"]["kind"] == "bool"
    assert rows["FILE_WATCH_ENABLED"]["field"] == "file_watch_enabled"
    assert rows["FILE_WATCH_ENABLED"]["default"] == rows["FILE_WATCH_ENABLED"]["value"]


# -- 单注册表：派生视图的两把守卫 ----------------------------------------------------------


def test_runtime_view_matches_frozen_golden() -> None:
    """**渲染快照不变**：视图从手写清单改为注册表派生，payload 必须逐字节等价。

    金样是迁移前的 HEAD 代码导出的（`runtime_view_golden.json`，11 组 32 行）。行序、
    label、note、kind、choices 全在其中 —— 派生实现错任何一处（顺序、只读标记、动态
    文案）这里就红。**有意变更界面时**重新导出金样并在同一笔提交里把 diff 说清楚：
    金样是"这一页长什么样"的合同，不是绊脚石。
    """
    golden_path = pathlib.Path(__file__).parent / "runtime_view_golden.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    now = runtime_payload(Settings())
    assert now == golden, (
        "运行环境视图与冻结金样不一致 —— 是注册表派生错了，还是有人有意改了界面"
        "（后者请同笔更新金样并写明变更）"
    )


def test_a_new_registry_entry_reaches_the_view_without_touching_the_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收「新增配置项只改 config.py + FieldSpec 两处」的机器化那一半。

    从前手写第三份行清单在 settings.py：新增字段漏改它，页面只是**静默缺一行**，
    没有任何信号。现在行由注册表派生 —— 这条用例往注册表末尾塞一个新条目（模拟
    "config.py 里已有字段 + FieldSpec 登记"这两处），断言视图**自己**多出这一行。
    """
    from rolecard_agent.core import runtime_settings as rs

    extra = rs.FieldSpec(
        "max_image_bytes",
        "MAX_IMAGE_BYTES",
        "int",
        label="图片大小上限",
        note="上传原图的字节上限",
        group="limit",
    )
    monkeypatch.setattr(rs, "RUNTIME_FIELDS", rs.RUNTIME_FIELDS + (extra,))
    payload = runtime_payload(Settings())
    groups = {g["key"]: g for g in payload["groups"]}  # type: ignore[index]
    rows = {r["key"]: r for r in groups["limit"]["items"]}  # type: ignore[index]
    assert "MAX_IMAGE_BYTES" in rows, "新登记的条目没出现在视图里 —— 视图还在手抄？"
    assert rows["MAX_IMAGE_BYTES"]["label"] == "图片大小上限"
    assert rows["MAX_IMAGE_BYTES"]["kind"] == "int"
